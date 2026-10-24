#!/usr/bin/env python3
"""下载固定版本并校验 SHA256；依赖只放在实验目录。"""
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent
for name, item in json.loads((ROOT / "sources.lock.json").read_text()).items():
    if item.get("kind") == "file":
        source = ROOT / "_deps" / f"{name}.c"
        source.parent.mkdir(exist_ok=True)
        if not source.exists():
            subprocess.run(["curl", "-fsSL", "--max-time", "120", item["url"], "-o", str(source)], check=True)
        if hashlib.sha256(source.read_bytes()).hexdigest() != item["sha256"]:
            raise SystemExit(f"{name}: SHA256 不匹配")
        print(f"{name}: 已校验独立源码")
        continue
    archive = ROOT / "_deps" / f"{name}.tar.gz"
    archive.parent.mkdir(exist_ok=True)
    if not archive.exists():
        subprocess.run(["curl", "-fsSL", "--max-time", "120", item["url"], "-o", str(archive)], check=True)
    if hashlib.sha256(archive.read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit(f"{name}: SHA256 不匹配")
    destination = ROOT / "_deps" / name
    if not destination.exists():
        destination.mkdir()
        with tarfile.open(archive) as source:
            for member in source.getmembers():
                parts = Path(member.name).parts[1:]
                if not parts:
                    continue
                if member.issym() or member.islnk() or ".." in parts or member.name.startswith("/"):
                    raise SystemExit(f"{name}: 不安全的归档成员")
                member.name = str(Path(*parts))
                source.extract(member, destination, filter="data")
    # 上游 POSIX KV 用 0660 创建目录，Linux 无执行权限，无法保存配对信息。
    if name == "iot_connect":
        # CoAP 扩展值来自任意字节偏移，不能强转为对齐整数指针。
        # Zig/musl 的 UBSan 会在真实云端控制报文触发 SIGABRT。
        path = destination / "core/wifi/infrastructure/transport/coap/codec/coap_codec_comm.c"
        text = path.read_text()
        for old, new in (
            ("IotcNtohs(*((uint16_t *)&raw->data[*pos]))",
             "(((uint32_t)raw->data[*pos] << 8) | raw->data[*pos + 1])"),
            ("IotcNtohl(*((uint32_t *)&raw->data[*pos]))",
             "(((uint32_t)raw->data[*pos] << 24) | ((uint32_t)raw->data[*pos + 1] << 16) | "
             "((uint32_t)raw->data[*pos + 2] << 8) | raw->data[*pos + 3])"),
        ):
            if old not in text and new not in text:
                raise SystemExit("CoAP 非对齐读取补丁上下文不匹配")
            text = text.replace(old, new)
        path.write_text(text)
        # 控制应答重写的序号位于已编码报文内部，同样没有整数对齐保证。
        for relative in (
            "core/wifi/application/cloud/link/sess/tls/m2m_cloud_sess.c",
            "core/wifi/application/cloud/link/sess/tcp/m2m_cloud_tcp_sess.c",
        ):
            path = destination / relative
            text = path.read_text()
            old = "    *(uint32_t *)seqOption->value.data = IotcHtonl(*seq);"
            new = """    /* POC_UNALIGNED_SEQUENCE_WRITE */
    uint8_t *seqBytes = (uint8_t *)seqOption->value.data;
    seqBytes[0] = (uint8_t)(*seq >> 24);
    seqBytes[1] = (uint8_t)(*seq >> 16);
    seqBytes[2] = (uint8_t)(*seq >> 8);
    seqBytes[3] = (uint8_t)*seq;"""
            if old not in text and new not in text:
                raise SystemExit("云端序号非对齐写入补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        path = destination / "core/wifi/infrastructure/utils/seq_num_utils.c"
        text = path.read_text()
        for old, new in (
            ("IotcNtohs(*(uint16_t *)data)", "(((uint32_t)data[0] << 8) | data[1])"),
            ("IotcNtohl(*(uint32_t *)data)",
             "(((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) | ((uint32_t)data[2] << 8) | data[3])"),
        ):
            if old not in text and new not in text:
                raise SystemExit("序号非对齐读取补丁上下文不匹配")
            text = text.replace(old, new)
        path.write_text(text)
        path = destination / "core/wifi/infrastructure/transport/coap/codec/coap_codec_udp.c"
        text = path.read_text()
        old = "IotcNtohs(*(uint16_t *)&raw->data[*pos])"
        new = "(((uint16_t)raw->data[*pos] << 8) | raw->data[*pos + 1])"
        if old not in text and new not in text:
            raise SystemExit("UDP 消息编号非对齐读取补丁上下文不匹配")
        path.write_text(text.replace(old, new))
        path = destination / "adapter/kv/posix/iotc_kv.c"
        text = path.read_text()
        if "mkdir(dir, 0660)" not in text and "mkdir(dir, 0700)" not in text:
            raise SystemExit("KV 补丁上下文不匹配")
        changed = text.replace("mkdir(dir, 0660)", "mkdir(dir, 0700)")
        if changed != text:
            path.write_text(changed)
        # 本地控协商会派生会话密钥；上游 INFO 日志不能输出这个值。
        path = destination / "core/wifi/application/local_control/local_ctl_coap_api.c"
        text = path.read_text()
        old = '    IOTC_LOGI("%s localCtlKey:[%s]", __func__, localCtlKeyStr);'
        new = '    /* POC_LOCAL_KEY_REDACTED: never log derived session keys. */'
        if old not in text and new not in text:
            raise SystemExit("本地会话密钥日志补丁上下文不匹配")
        path.write_text(text.replace(old, new))
        # App 实测请求 cloudSetup；仅兼容实验构建启用别名，不改变上游默认入口。
        path = destination / "core/wifi/application/lan_search/lan_search_coap_stack.c"
        text = path.read_text()
        marker = "/* POC_AILIFE_DISCOVERY_ALIAS */"
        if marker not in text:
            old = "static const char *uriWhiteList[] = { STR_URI_LAN_SEARCH, STR_URI_SPKEK_V2, NULL };"
            new = '''/* POC_AILIFE_DISCOVERY_ALIAS */
    static const char *uriWhiteList[] = { STR_URI_LAN_SEARCH,
#ifdef IOTC_POC_HILINK_DISCOVERY_COMPAT
        ".well-known/core?st=cloudSetup",
#endif
        STR_URI_SPKEK_V2, NULL };'''
            resource = "{ UTILS_BIT(COAP_METHOD_TYPE_GET), STR_URI_LAN_SEARCH, NULL, LanSearchCoapSearchHandler },"
            extra = '''
#ifdef IOTC_POC_HILINK_DISCOVERY_COMPAT
        { UTILS_BIT(COAP_METHOD_TYPE_GET), ".well-known/core?st=cloudSetup", NULL, LanSearchCoapSearchHandler },
#endif'''
            if old not in text or resource not in text:
                raise SystemExit("发现入口补丁上下文不匹配")
            path.write_text(text.replace(old, new).replace(resource, resource + extra))
        text = path.read_text()
        if ".port = IOTC_POC_LAN_PORT" not in text and "PocGetLanPort(IOTC_POC_LAN_PORT)" not in text:
            old = ".port = LAN_SEARCH_PORT,"
            if old not in text:
                raise SystemExit("独立 LAN 端口补丁上下文不匹配")
            path.write_text(text.replace(old, ".port = IOTC_POC_LAN_PORT,"))
        text = path.read_text()
        if "/* POC_RUNTIME_LAN_PORT */" not in text:
            old = ".port = IOTC_POC_LAN_PORT,"
            if old not in text:
                raise SystemExit("运行时 LAN 端口补丁上下文不匹配")
            text = text.replace(old, ".port = PocGetLanPort(IOTC_POC_LAN_PORT),")
            path.write_text('/* POC_RUNTIME_LAN_PORT */\n#include "poc_network.h"\n' + text)
        # 发现阶段声明真实存在的 SPEKE/v2、CloudSetup/v2 认证入口，业务服务仍由模型保存。
        path = destination / "core/wifi/application/lan_search/lan_search_coap_api.c"
        text = path.read_text()
        marker = "/* POC_AILIFE_DISCOVERY_SERVICES */"
        if marker not in text:
            old = "    const IotcServiceInfo *svcInfo = ModelGetSvcInfo(&svcNum);"
            new = '''    /* POC_AILIFE_DISCOVERY_SERVICES */
#ifdef IOTC_POC_HILINK_DISCOVERY_COMPAT
    static const IotcServiceInfo setupServices[] = {
        { "policy", "kitAuth" }, { "speke", "speke" }, { "cloudSetup", "cloudSetup" }
    };
    svcNum = ARRAY_SIZE(setupServices);
    const IotcServiceInfo *svcInfo = setupServices;
#else
    const IotcServiceInfo *svcInfo = ModelGetSvcInfo(&svcNum);
#endif'''
            if old not in text:
                raise SystemExit("发现服务列表补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        elif '{ "policy", "kitAuth" }' not in text:
            old = '{ "speke", "speke" }, { "cloudSetup", "cloudSetup" }'
            if old not in text:
                raise SystemExit("PIN 认证声明补丁上下文不匹配")
            path.write_text(text.replace(old, '{ "policy", "kitAuth" }, ' + old))
        # 旧 HiLink 发现帧不带 OpenHarmony discType 标记，保持已验证的线格式。
        text = path.read_text()
        marker = "/* POC_AILIFE_LEGACY_FRAME */"
        if marker not in text:
            old = "    /* 高四bit预留0110保证为可见字符，低四bit依次为softap coap sle ble发现能力 */"
            new = '''    /* POC_AILIFE_LEGACY_FRAME */
#ifdef IOTC_POC_HILINK_DISCOVERY_COMPAT
    return true;
#endif
''' + old
            if old not in text:
                raise SystemExit("发现帧兼容补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        # 与 SoftAP 的 AiLife 路径一致，LAN 配网也必须保存云端绑定信息。
        text = path.read_text()
        if "/* POC_AILIFE_LAN_CLOUD_BIND */" not in text:
            old = "    int32_t ret = DevSvcProxyRecvAuthInfo(dataJson);"
            new = '''    /* POC_AILIFE_LAN_CLOUD_BIND */
#if IOTC_CONF_AILIFE_SUPPORT && defined(IOTC_CONNECT_WIFI_CLOUD_SUPPORT)
    int32_t ret = DevSvcProxyRecvBindInfo(dataJson);
#else
    int32_t ret = DevSvcProxyRecvAuthInfo(dataJson);
#endif'''
            if text.count(old) != 1:
                raise SystemExit("LAN 云端绑定补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        # 新 peer 入表后未更新计数，下一包会被误判为“无 peer”并重建 SPEKE 上下文。
        path = destination / "core/wifi/application/lan_search/lan_search_peer_mngr.c"
        text = path.read_text()
        if "/* POC_LAN_PEER_COUNT_FIX */" not in text:
            old = "    *peer = newPeer;\n    return IOTC_OK;"
            new = '''    /* POC_LAN_PEER_COUNT_FIX */
    ctx->peerManager.curPeerNum = UtilsHashMapLength(ctx->peerManager.peerMap);
    *peer = newPeer;
    return IOTC_OK;'''
            if text.count(old) != 1:
                raise SystemExit("LAN 会话计数补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        # 2026 TCP/PSK 路径不能用于旧版 HiLink TLS；恢复上游原有 TLS 激活/登录分支。
        path = destination / "core/wifi/application/cloud/m2m_cloud_fsm.c"
        text = path.read_text()
        if "/* POC_TLS_REGISTER_FLOW */" not in text:
            old = "    /* 此处发送CSM */\n    M2mCloudSendCSM(ctx);\n    return M2M_CLOUD_FSM_STATE_PSK;"
            new = '''    /* POC_TLS_REGISTER_FLOW */
#if IOTC_CONF_TCP_SUPPORT
    M2mCloudSendCSM(ctx);
    return M2M_CLOUD_FSM_STATE_PSK;
#else
    return UTILS_IS_BIT_SET(ctx->bitMap, M2M_CLOUD_CTX_BIT_REGISTER) ?
        M2M_CLOUD_FSM_STATE_REGISTER : M2M_CLOUD_FSM_STATE_LOGIN;
#endif'''
            if text.count(old) != 1:
                raise SystemExit("TLS 激活流程补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        # TLS 激活结构与同一上游历史版本一致；TCP 的 devId/reqId 不带入旧协议。
        path = destination / "core/wifi/application/cloud/biz/m2m_cloud_reg.c"
        text = path.read_text()
        if "/* POC_TLS_ACTIVATE_SCHEMA */" not in text:
            old = '''        .opBitMap = UTILS_BIT(CLOUD_OPTION_BIT_SEQ_NUM_ID) |
                    UTILS_BIT(CLOUD_OPTION_BIT_REQ_ID) |
                    UTILS_BIT(CLOUD_OPTION_BIT_DEV_ID),'''
            new = '''        /* POC_TLS_ACTIVATE_SCHEMA */
#if IOTC_CONF_TCP_SUPPORT
''' + old + '''
#else
        .opBitMap = UTILS_BIT(CLOUD_OPTION_BIT_SEQ_NUM_ID),
#endif'''
            payload = '''        ret = IotcJsonAddStr2Obj(rootJson, STR_JSON_DEVID, ctx->authInfo.regInfo.devId);
        if (ret != IOTC_OK) {
            IOTC_LOGW("add devid error %d", ret);
            break;
        }'''
            if text.count(old) != 1 or text.count(payload) != 1:
                raise SystemExit("TLS 激活字段补丁上下文不匹配")
            path.write_text(text.replace(old, new).replace(payload, "#if IOTC_CONF_TCP_SUPPORT\n" + payload + "\n#endif"))
        path = destination / "core/wifi/application/cloud/biz/m2m_cloud_utils.c"
        text = path.read_text()
        if "/* POC_TLS_HILINK_VERSION */" not in text:
            old = "        {STR_JSON_MAC, macStr},"
            new = old + '''
        /* POC_TLS_HILINK_VERSION */
#if !IOTC_CONF_TCP_SUPPORT
        {STR_JSON_HIV, HILINK_VERSION},
#endif'''
            if text.count(old) != 1:
                raise SystemExit("TLS 协议版本字段补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        path = destination / "core/wifi/application/cloud/biz/m2m_cloud_send.c"
        text = path.read_text()
        if "/* POC_TLS_REQUEST_OPTIONS */" not in text:
            old = "    uint32_t opNum;\n    CoapOption *options = BuildCloudOption(ctx, option, &opNum);"
            new = '''    /* POC_TLS_REQUEST_OPTIONS */
#if !IOTC_CONF_TCP_SUPPORT
    CloudOption tlsOption = *option;
    tlsOption.opBitMap &= ~(UTILS_BIT(CLOUD_OPTION_BIT_REQ_ID) | UTILS_BIT(CLOUD_OPTION_BIT_DEV_ID));
    option = &tlsOption;
#endif
    uint32_t opNum;
    CoapOption *options = BuildCloudOption(ctx, option, &opNum);'''
            if text.count(old) != 1:
                raise SystemExit("TLS 请求选项补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        # TLS 控制响应直接返回特征对象；不走 TCP 桥设备的异步响应节点。
        path = destination / "core/wifi/application/cloud/biz/m2m_cloud_ctl.c"
        text = path.read_text()
        if "/* POC_TLS_CONTROL_RESPONSE */" not in text:
            start = text.index("static int32_t SendCloudCtlMsgResp(")
            end = text.index("static void M2mCloudCoapControlHandler", start)
            old = text[start:end]
            new = '''/* POC_TLS_CONTROL_RESPONSE */
#if !IOTC_CONF_TCP_SUPPORT
static int32_t SendCloudCtlMsgResp(
    CoapEndpoint *endpoint, const CoapPacket *req, const SocketAddr *addr, const M2mCloudContext *ctx)
{
    IotcJson *array = ParseCloudCtlMsg(req);
    if (array == NULL) {
        return IOTC_ERROR;
    }
    /* POC_SAFE_CONTROL_TRACE: names only; never log payload/PIN/device IDs. */
    IotcJson *first = IotcJsonGetArrayItem(array, 0);
    const char *sid = IotcJsonGetStr(IotcJsonGetObj(first, STR_JSON_SID));
    const char *known = "other";
    const char *names[] = {"switch", "brightness", "cct", "netInfo", "addDevice", "netTest", "rssiData", "sceneOperation", "update"};
    for (uint32_t i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        if (sid != NULL && strcmp(sid, names[i]) == 0) { known = names[i]; break; }
    }
    IotcJson *input = IotcJsonGetObj(first, STR_JSON_DATA);
    IOTC_LOGN("POC_CONTROL_SERVICE sid=%s mode=%d type=%d preInstaller=%d devList=%d", known,
        IotcJsonHasObj(input, "mode"), IotcJsonHasObj(input, "type"),
        IotcJsonHasObj(input, "preInstaller"), IotcJsonHasObj(input, "devList"));
    IotcJson *response = NULL;
    int32_t ret;
    if (req->header.code == COAP_METHOD_TYPE_GET) {
        IotcJson *states = NULL;
        ret = DevSvcProxyCtlGetCharStates(array, &states);
        if (ret == IOTC_OK) {
            IotcJson *item = IotcJsonGetArrayItem(states, 0);
            IotcJson *data = item == NULL ? NULL : IotcJsonGetObj(item, STR_JSON_DATA);
            response = data == NULL ? NULL : IotcDuplicateJson(data, true);
            if (response == NULL) {
                ret = IOTC_ERROR;
            }
        }
        IotcJsonDelete(states);
    } else if (req->header.code == COAP_METHOD_TYPE_POST || req->header.code == COAP_METHOD_TYPE_PUT) {
        ret = DevSvcProxyCtlPutCharStates(array, NULL);
    } else {
        ret = IOTC_ERR_NOT_SUPPORT;
    }
    if (response == NULL) {
        response = UtilsJsonCreateErrcode(ret);
    }
    IotcJsonDelete(array);
    if (response == NULL) {
        return IOTC_ERROR;
    }
    IOTC_LOGN("POC_TLS_CONTROL method=%u result=%d", req->header.code, ret);
    int32_t sent = BuildCloudCtlRespMsg(endpoint, req, addr, ctx, response);
    IotcJsonDelete(response);
    return sent;
}
#else
''' + old + "#endif\n\n"
            text = text[:start] + new + text[end:]
            start = text.index("static int32_t BuildCloudCtlRespMsg(")
            end = text.index("/* POC_TLS_CONTROL_RESPONSE */", start)
            block = text[start:end]
            option_start = block.index("            {COAP_OPTION_TYPE_URI_PATH,")
            option_end = block.index("            {COAP_OPTION_TYPE_ACCESS_TOKEN_ID", option_start)
            block = block[:option_start] + "#if IOTC_CONF_TCP_SUPPORT\n" + block[option_start:option_end] + "#endif\n" + block[option_end:]
            text = text[:start] + block + text[end:]
            path.write_text(text)
        # 仅记录实验进程收到的旧桥控制请求；载荷在私有文件中，不写入 SDK 日志。
        path = destination / "core/wifi/application/cloud/biz/m2m_cloud_ctl.c"
        text = path.read_text()
        if "/* POC_BRIDGE_REQUEST_TRACE */" not in text:
            marker = "/* POC_TLS_CONTROL_RESPONSE */"
            trace = '''/* POC_BRIDGE_REQUEST_TRACE */
#if !IOTC_CONF_TCP_SUPPORT
static void PocTraceBridgeRequest(const CoapPacket *req)
{
    uint32_t segments = 0;
    const CoapOption *uri = CoapUtilsFindOption(req, COAP_OPTION_TYPE_URI_PATH, &segments);
    IOTC_LOGN("POC_BRIDGE_REQUEST method=%u uri_segments=%u payload_bytes=%u",
        req->header.code, segments, req->payload.len);
    uint32_t targetCount = 0;
    const CoapOption *target = CoapUtilsFindOption(req, COAP_OPTION_TYPE_DEV_ID, &targetCount);
    if (uri != NULL && segments == 2 && target != NULL && targetCount == 1) {
        bool same = uri[0].value.len == target->value.len &&
            memcmp(uri[0].value.data, target->value.data, target->value.len) == 0;
        IOTC_LOGN("POC_BRIDGE_TARGET uri_length=%u header_length=%u same=%d",
            uri[0].value.len, target->value.len, same);
    }
    for (uint32_t i = 0; uri != NULL && i < segments; ++i) {
        uint32_t size = uri[i].value.len;
        const char *name = (const char *)uri[i].value.data;
        bool safe = name != NULL && size <= 40;
        for (uint32_t j = 0; safe && j < size; ++j) {
            safe = name[j] == '.' || (name[j] >= 'a' && name[j] <= 'z') || (name[j] >= 'A' && name[j] <= 'Z');
        }
        if (safe) IOTC_LOGN("POC_BRIDGE_URI segment=%u name=%.*s", i, (int)size, name);
    }
    if (getenv("POC_TRACE_CHILD_CONTROL") != NULL && segments > 1 && req->payload.data != NULL &&
        req->payload.len > 0 && req->payload.len <= 32768) {
        int fd = open("control-request.private.jsonl", O_WRONLY | O_CREAT | O_APPEND | O_NOFOLLOW, 0600);
        if (fd >= 0) {
            if (fchmod(fd, 0600) == 0) {
                struct iovec line[] = {{(void *)req->payload.data, req->payload.len}, {(void *)"\\n", 1}};
                ssize_t written = writev(fd, line, 2);
                (void)written;
            }
            close(fd);
        }
    }
}
#endif
'''
            if text.count(marker) != 1:
                raise SystemExit("桥控制诊断补丁上下文不匹配")
            text = text.replace(marker, trace + marker)
            text = text.replace("    IotcJson *array = ParseCloudCtlMsg(req);",
                                "    PocTraceBridgeRequest(req);\n    IotcJson *array = ParseCloudCtlMsg(req);", 1)
            path.write_text('#include <stdlib.h>\n#include <fcntl.h>\n#include <sys/stat.h>\n#include <sys/uio.h>\n#include <unistd.h>\n' + text)
        # 旧 TLS 子设备命令使用 target/service 两段 URI；服务名取末段。
        text = path.read_text()
        if "/* POC_CHILD_CONTROL_URI */" not in text:
            old = "    const CoapOption *uriOption = CoapUtilsFindOption(req, type, &seg);"
            new = old + '''
    /* POC_CHILD_CONTROL_URI */
#if !IOTC_CONF_TCP_SUPPORT
    if (type == COAP_OPTION_TYPE_URI_PATH && uriOption != NULL && seg == 2) {
        uriOption += 1;
        seg = 1;
    }
#endif'''
            if text.count(old) != 1:
                raise SystemExit("子设备控制 URI 补丁上下文不匹配")
            text = text.replace(old, new)
            old = 'if (*uriOpt == NULL || seg != 1 || (*uriOpt)->value.data == NULL || (*uriOpt)->value.len == 0)'
            new = 'if (*uriOpt == NULL || (seg != 1 && (IOTC_CONF_TCP_SUPPORT || seg != 2)) || (*uriOpt)->value.data == NULL || (*uriOpt)->value.len == 0)'
            if text.count(old) != 1:
                raise SystemExit("子设备应答 URI 校验补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        # TLS 已同步应答控制命令；后续状态消息是上报，不走 TCP 的待应答节点表。
        path = destination / "core/wifi/application/cloud/m2m_cloud_svc.c"
        text = path.read_text()
        if "/* POC_TLS_CONTROL_REPORT */" not in text:
            start = "static int32_t M2mCloudIsRespMessageType(const IotcJson *msg)"
            end = "static int32_t M2mCloudDeviceReportMessageHandler("
            if text.count(start) != 1 or text.count(end) != 1:
                raise SystemExit("TLS 控制后上报补丁上下文不匹配")
            text = text.replace(start, "/* POC_TLS_CONTROL_REPORT */\n#if IOTC_CONF_TCP_SUPPORT\n" + start)
            text = text.replace(end, "#endif\n\n" + end)
            old = '''    if (M2mCloudIsRespMessageType(req->msg) == IOTC_OK) {
        ret = M2mCloudResponseMessage(req->msg);
    } else {'''
            new = '''#if IOTC_CONF_TCP_SUPPORT
    if (M2mCloudIsRespMessageType(req->msg) == IOTC_OK) {
        ret = M2mCloudResponseMessage(req->msg);
    } else
#endif
    {'''
            if text.count(old) != 1:
                raise SystemExit("TLS 状态消息分流补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        path = destination / "core/wifi/application/cloud/biz/m2m_cloud_ctl.c"
        text = path.read_text()
        if "/* POC_CHILD_TARGET_VALIDATE */" not in text:
            old = "    IotcJson *payloadObj = BuildPayloadObject(req, svcId, msgId, devId);"
            new = '''    /* POC_CHILD_TARGET_VALIDATE */
#if !IOTC_CONF_TCP_SUPPORT
    uint32_t targetSegments = 0;
    const CoapOption *targetUri = CoapUtilsFindOption(req, COAP_OPTION_TYPE_URI_PATH, &targetSegments);
    if (targetSegments == 2 && (targetUri == NULL || targetUri[0].value.len != strlen(devId) ||
        memcmp(targetUri[0].value.data, devId, targetUri[0].value.len) != 0)) {
        IOTC_LOGW("child URI/header target mismatch");
        CleanupParseResources(array, msgId, devId, NULL);
        return NULL;
    }
#endif
''' + old
            if text.count(old) != 1:
                raise SystemExit("子设备目标校验补丁上下文不匹配")
            path.write_text(text.replace(old, new))
        # TLS 主动上报与原有 TLS 请求采用相同选项，不附带新 TCP 桥设备选项。
        path = destination / "core/wifi/application/cloud/biz/m2m_cloud_report.c"
        text = path.read_text()
        if "/* POC_TLS_REPORT_OPTIONS */" not in text:
            old = '''        {COAP_OPTION_TYPE_REQ_ID, {(const uint8_t *)ctx->reqId, strlen(ctx->reqId)}},
        {COAP_OPTION_TYPE_DEV_ID,
            {(const uint8_t *)ctx->authInfo.loginInfo.devId, strlen(ctx->authInfo.loginInfo.devId)}},'''
            if text.count(old) != 1:
                raise SystemExit("TLS 状态上报选项补丁上下文不匹配")
            new = "/* POC_TLS_REPORT_OPTIONS */\n#if IOTC_CONF_TCP_SUPPORT\n" + old + "\n#endif"
            path.write_text(text.replace(old, new))
        # 桥设备结构启用后，根设备 ID="0" 必须按字符串判断，不能把 cJSON 指针当 char*。
        text = path.read_text()
        old = 'strcmp(IotcJsonGetObj(item, STR_JSON_DEVID), "0")'
        new = 'strcmp(IotcJsonGetStr(IotcJsonGetObj(item, STR_JSON_DEVID)), "0")'
        if old not in text and new not in text:
            raise SystemExit("子设备上报路由补丁上下文不匹配")
        if old in text:
            path.write_text(text.replace(old, new))
        # 网关模式的转换层还为 devId/msgId 分配内存，释放时也需成对回收。
        path = destination / "core/device/model/char_state_mdl.c"
        text = path.read_text()
        if "/* POC_CHILD_STATE_FREE */" not in text:
            old = "        UTILS_FREE_2_NULL((*states)[i].svcId);"
            if text.count(old) != 1:
                raise SystemExit("子设备状态释放补丁上下文不匹配")
            new = old + '''
        /* POC_CHILD_STATE_FREE */
#if IOTC_CONF_DEV_TYPE == 2
        UTILS_FREE_2_NULL((*states)[i].devId);
        UTILS_FREE_2_NULL((*states)[i].msgId);
#endif'''
            path.write_text(text.replace(old, new))
        # 消息头含 8 字节函数指针 + 4 字节长度，原柔性数组只对齐到 4 字节。
        # 消费者把它解释为含指针的 ExecutorParam，需要标准最大对齐。
        path = destination / "core/infrastructure/frameworks/event_loop/event_source_msg_queue.c"
        text = path.read_text()
        old = "    uint8_t msg[];"
        new = "    _Alignas(max_align_t) uint8_t msg[];"
        if old not in text and new not in text:
            raise SystemExit("消息对齐补丁上下文不匹配")
        changed = text.replace(old, new)
        if "#include <stddef.h>" not in changed:
            changed = "#include <stddef.h>\n" + changed
        if changed != text:
            path.write_text(changed)
        # 原宏访问空指针成员并在无关指针间做减法；Zig 运行检查会中止 SDK。
        path = destination / "core/infrastructure/utils/include/utils_list.h"
        text = path.read_text()
        old = "((type *)((char *)(memberAddr) - (char *)(&((type *)0)->member)))"
        new = "((type *)((char *)(memberAddr) - offsetof(type, member)))"
        if old not in text and new not in text:
            raise SystemExit("链表宏补丁上下文不匹配")
        changed = text.replace(old, new)
        if "#include <stddef.h>" not in changed:
            changed = changed.replace("#define UTILS_LIST_H", "#define UTILS_LIST_H\n#include <stddef.h>")
        if changed != text:
            path.write_text(changed)
    print(f"{name}: 已校验并解压")
