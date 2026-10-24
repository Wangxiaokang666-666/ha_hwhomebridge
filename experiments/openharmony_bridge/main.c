/* 独立实验 SDK 网关；HA 子设备仅在显式开启第二阶段配置后接入。 */
#define _POSIX_C_SOURCE 200809L
#define _XOPEN_SOURCE 700
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <limits.h>
#include <time.h>
#include <unistd.h>
#include "cJSON.h"
#include "iotc_oh_device.h"
#include "iotc_oh_sdk.h"
#include "iotc_oh_wifi.h"
#include "iotc_event.h"
#include "iotc_network.h"
#include "poc_network.h"
#include "virtual_child.h"
#include "ha_children.h"
#include "iotc_errcode.h"
#include "iotc_svc.h"
#include "service_proxy.h"
#include "sched_timer.h"
#include "iotc_svc_dev.h"
#include "config_register_info.h"
#include "m2m_cloud_ctx.h"

static pthread_mutex_t state_mutex = PTHREAD_MUTEX_INITIALIZER;
static int switch_on;
static volatile sig_atomic_t quitting;
static bool sdk_started;
static cJSON *config;
static unsigned char ac_key[IOTC_AC_KEY_LEN];
static const char *pin;
static unsigned char pin_bytes[IOTC_PINCODE_LEN];
static time_t pin_expires_at;
static char *ca_pem;
static const char *ca_list[1];
static const IotcServiceInfo services[] = {{"switch", "switch"}};
static const IotcServiceInfo gateway_services[] = {{"netInfo", "netInfo"}};
static const IotcServiceInfo *sdk_services = services;

static void event(const char *name, int result) {
    printf("POC {\"event\":\"%s\",\"result\":%d}\n", name, result);
    fflush(stdout);
}
static void signal_quit(int reason) { (void)reason; quitting = 1; }
static int32_t reboot(int32_t reason) { event("sdk_reboot_requested", reason); quitting = 1; return 0; }

static char *read_file(const char *path, size_t maximum) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW);
    if (fd < 0) return NULL;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_size <= 0 || (size_t)st.st_size > maximum) {
        close(fd); return NULL;
    }
    char *data = calloc((size_t)st.st_size + 1, 1);
    if (!data) { close(fd); return NULL; }
    size_t pos = 0;
    while (pos < (size_t)st.st_size) {
        ssize_t n = read(fd, data + pos, (size_t)st.st_size - pos);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { free(data); close(fd); return NULL; }
        pos += (size_t)n;
    }
    if (memchr(data, 0, pos)) { free(data); close(fd); return NULL; }
    close(fd);
    return data;
}
static int validate_enum(const char *data, uint32_t len, const char *key, int minimum, int maximum, int *value) {
    if (!data || !len || len > 128 || memchr(data, 0, len)) return -1;
    char buf[129]; memcpy(buf, data, len); buf[len] = 0;
    cJSON *json = cJSON_ParseWithOpts(buf, NULL, true);
    if (!json || !cJSON_IsObject(json) || cJSON_GetArraySize(json) != 1) { cJSON_Delete(json); return -1; }
    cJSON *item = cJSON_GetObjectItemCaseSensitive(json, key);
    int valid = cJSON_IsNumber(item) && item->valuedouble >= minimum && item->valuedouble <= maximum &&
        item->valuedouble == item->valueint;
    if (valid) *value = item->valueint;
    cJSON_Delete(json);
    return valid ? 0 : -1;
}
static int validate_switch(const char *data, uint32_t len, int *on) {
    return validate_enum(data, len, "on", 0, 1, on);
}
static int save_enum(const char *path, const char *temporary, const char *key, int value) {
    int fd = open(temporary, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    if (fd < 0) return -1;
    if (fchmod(fd, 0600)) { close(fd); return -1; }
    char data[64]; int len = snprintf(data, sizeof(data), "{\"%s\":%d}\n", key, value);
    int ret = 0;
    if (write(fd, data, (size_t)len) != len || fsync(fd)) ret = -1;
    if (close(fd)) ret = -1;
    if (ret || rename(temporary, path)) return -1;
    int directory = open(".", O_RDONLY | O_DIRECTORY);
    if (directory < 0) return -1;
    ret = fsync(directory); close(directory);
    return ret;
}
static int save_switch(int on) { return save_enum("switch.json", "switch.tmp", "on", on); }
static int32_t get_state(const IotcCharState state[], char *out[], uint32_t len[], uint32_t num) {
    if (!state || !out || !len || num != 1 || !state[0].svcId) return -1;
    if (PocHaMatches(state[0].devId)) return PocHaGet(&state[0], &out[0], &len[0]);
    if (PocChildMatches(state[0].devId)) return PocChildGet(&state[0], &out[0], &len[0]);
    if (PocChildEnabled() && !PocChildRootMatches(state[0].devId)) return -1;
    if (sdk_services == gateway_services && !strcmp(state[0].svcId, "netInfo")) {
        char ip[64], data[96];
        if (IotcGetLocalIp(ip, sizeof(ip))) return -1;
        snprintf(data, sizeof(data), "{\"IP\":\"%s\"}", ip);
        out[0] = strdup(data);
        if (!out[0]) return -1;
        len[0] = (uint32_t)strlen(out[0]); event("netinfo_read", 0);
        return 0;
    }
    if (sdk_services == gateway_services) return -1;
    if (strcmp(state[0].svcId, "switch")) return -1;
    pthread_mutex_lock(&state_mutex);
    int on = switch_on;
    char data[16]; snprintf(data, sizeof(data), "{\"on\":%d}", on);
    out[0] = strdup(data);
    pthread_mutex_unlock(&state_mutex);
    if (!out[0]) return -1;
    len[0] = (uint32_t)strlen(out[0]);
    event("service_read", on);
    return 0;
}
static int32_t report_all(void) {
    IotcCharState state = {.svcId = sdk_services[0].svcId};
    char *data = NULL; uint32_t len = 0;
    if (get_state(&state, &data, &len, 1)) return -1;
    state.data = data; state.len = len;
    int32_t ret = IotcOhDevReportCharState(&state, 1);
    free(data); event("sdk_report_queued_result", ret);
    return ret;
}
static int32_t put_state(const IotcCharState state[], uint32_t num) {
    if (state && num == 1 && PocHaMatches(state[0].devId)) return PocHaPut(&state[0]);
    if (state && num == 1 && PocChildMatches(state[0].devId)) return PocChildPut(&state[0]);
    if (PocChildEnabled()) return -1;
    int on;
    if (!state || num != 1 || !state[0].svcId || strcmp(state[0].svcId, "switch") ||
        validate_switch(state[0].data, state[0].len, &on)) return -1;
    pthread_mutex_lock(&state_mutex);
    int ret = save_switch(on);
    if (!ret) switch_on = on;
    pthread_mutex_unlock(&state_mutex);
    if (ret) return -1;
    event("service_write", on);
    /* SDK 回调内只持久化/应答；上报由主线程执行，避免回调重入。 */
    return 0;
}
static int32_t get_pin(uint8_t *buf, uint32_t len) {
    if (!pin || time(NULL) >= pin_expires_at) return IOTC_ERR_NOT_SUPPORT;
    if (!buf || len < IOTC_PINCODE_LEN) return IOTC_ERR_PARAM_INVALID;
    event("pin_callback_valid", 0);
    memcpy(buf, pin_bytes, IOTC_PINCODE_LEN); return 0;
}
static int32_t get_key(uint8_t *buf, uint32_t len) {
    if (!pin) return IOTC_ERR_NOT_SUPPORT;
    if (!buf || len < sizeof(ac_key)) return IOTC_ERR_PARAM_INVALID;
    memcpy(buf, ac_key, sizeof(ac_key)); return 0;
}
static int32_t get_ca(const char **list[], uint32_t *num) {
    if (!ca_pem) return IOTC_ERR_NOT_SUPPORT;
    if (!list || !num) return IOTC_ERR_PARAM_INVALID;
    *list = ca_list; *num = 1; return 0;
}
static int32_t wifi_enable(void) { return IotcGetNetworkState() == IOTC_NETWORK_CONNECTED ? 0 : -1; }
static void cloud_status(int32_t id, void *data) {
    (void)id; (void)data;
    static int last_bind = -99, last_reg = -99, last_state = -99;
    int bind = DevSvcProxyGetBindStatus(), reg = IsRegisterInfoExist();
    M2mCloudContext *ctx = GetM2mCloudCtx();
    int state = ctx->stateManager.fsmCtx ? ctx->stateManager.fsmCtx->cur : -99;
    if (bind != last_bind || reg != last_reg || state != last_state) {
        event("cloud_bind_status", bind); event("cloud_register_info_present", reg);
        event("cloud_fsm_state", state);
        last_bind = bind; last_reg = reg; last_state = state;
    }
}
static void sdk_event(int32_t id) {
    event("sdk_event", id);
    if (id == IOTC_SDK_AILIFE_EVENT_WIFI_NET_CONNECT) {
        /* 上游 OH Wi-Fi 入口只启动连接/云服务，LAN 服务需业务方显式启动。 */
        int32_t result = ServiceProxyStartService(IOTC_SERVICE_ID_LAN_SEARCH, NULL);
        event("lan_service_start", result);
        if (getenv("POC_CLOUD_STATUS"))
            event("cloud_status_timer", SchedTimerAdd(EVENT_SOURCE_TIMER_TYPE_REPEAT, cloud_status, 1000, NULL));
    }
    if (id == IOTC_SDK_AILIFE_EVENT_WIFI_UPLINK_REGISTERED) event("cloud_registered", 0);
    if (id == IOTC_SDK_AILIFE_EVENT_WIFI_UPLINK_ONLINE) { event("cloud_online", 0); PocChildCloudOnline(); PocHaCloudOnline(); }
    if (id == IOTC_SDK_AILIFE_EVENT_WIFI_UPLINK_OFFLINE) { event("cloud_offline", 0); PocChildCloudOffline(); PocHaCloudOffline(); }
}
static const char *field(const char *name) {
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(config, name);
    return cJSON_IsString(item) ? item->valuestring : NULL;
}
static int load_config(const char *path, bool live) {
    char *data = read_file(path, 65536);
    if (!data) return -1;
    config = cJSON_ParseWithOpts(data, NULL, true);
    free(data);
    const char *required[] = {"sn", "prodId", "model", "devTypeId", "devTypeName", "manuId", "manuName", "devName"};
    for (size_t i = 0; i < sizeof(required) / sizeof(required[0]); ++i) {
        const char *s = field(required[i]);
        if (!s || !s[0]) { fprintf(stderr, "缺少产品字段: %s\n", required[i]); return -1; }
    }
    /* 普通实验只用OHPOC；显式接管还必须由离线读取器确认原绑定。 */
    bool bound_mode = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "preserve_bound_identity"));
    if ((!bound_mode && strncmp(field("sn"), "OHPOC-", 6)) || strlen(field("sn")) > 39 ||
        strspn(field("sn"), "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") != strlen(field("sn"))) return -1;
    if (bound_mode && (!cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(config, "enable_virtual_child")) ||
        !field("expected_cloud_id_sha256") || strlen(field("expected_cloud_id_sha256")) != 64)) return -1;
    if (bound_mode && !live) return -1;
    if (!live) return 0;
    struct stat st;
    if (stat(path, &st) || (st.st_mode & 077) || st.st_uid != getuid()) {
        fprintf(stderr, "配对配置需归当前用户所有，权限为 0600\n"); return -1;
    }
    pin = field("pin"); const char *hex = field("ac_key_hex"); const char *ca = field("ca_file");
    if (!pin || strlen(pin) != 8 || strspn(pin, "0123456789") != 8 || !hex || strlen(hex) != 96 ||
        strspn(hex, "0123456789abcdefABCDEF") != 96 || !ca) {
        fprintf(stderr, "配对缺少有效 PIN、48 字节 AC Key 或 CA 文件\n"); return -1;
    }
    const cJSON *expiry = cJSON_GetObjectItemCaseSensitive(config, "pin_expires_at");
    pin_expires_at = cJSON_IsNumber(expiry) ? (time_t)expiry->valuedouble : time(NULL) + 300;
    memcpy(pin_bytes, pin, sizeof(pin_bytes));
    /* SDK PIN 回调是 8 字节缓冲区；自动配对产品可能使用非 ASCII PIN。 */
    const char *pin_hex = field("pin_bytes_hex");
    if (pin_hex) {
        if (strlen(pin_hex) != 2 * sizeof(pin_bytes) ||
            strspn(pin_hex, "0123456789abcdefABCDEF") != 2 * sizeof(pin_bytes)) return -1;
        for (size_t i = 0; i < sizeof(pin_bytes); ++i) {
            unsigned int value;
            if (sscanf(pin_hex + 2 * i, "%2x", &value) != 1) return -1;
            pin_bytes[i] = (unsigned char)value;
        }
    }
    for (size_t i = 0; i < sizeof(ac_key); ++i) {
        unsigned int value; if (sscanf(hex + 2 * i, "%2x", &value) != 1) return -1;
        ac_key[i] = (uint8_t)value;
    }
    ca_pem = read_file(ca, 262144);
    if (!ca_pem || !strstr(ca_pem, "-----BEGIN CERTIFICATE-----")) return -1;
    ca_list[0] = ca_pem;
    return 0;
}
/* 启动任何SDK任务前，用同目录的原生离线探针确认已有根身份。 */
static int verify_bound(const char *executable, const char *gateway) {
    if (!cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(config, "preserve_bound_identity"))) return 0;
    char probe[PATH_MAX], store[PATH_MAX];
    /* 管理层的活动目录共享原 KV；探针仍只接收真实目录并检查私有权限。 */
    if (!realpath("sdk-state", store)) return -1;
    if (strlen(executable) + 32 >= sizeof(probe)) return -1;
    strcpy(probe, executable);
    char *slash = strrchr(probe, '/');
    if (!slash) return -1;
    strcpy(slash + 1, "identity_state_probe");
    struct stat info;
    if (lstat(probe, &info) || !S_ISREG(info.st_mode) || info.st_uid != getuid() || (info.st_mode & 077)) return -1;
    int descriptors[2];
    if (pipe(descriptors)) return -1;
    pid_t pid = fork();
    if (!pid) {
        close(descriptors[0]);
        if (dup2(descriptors[1], STDOUT_FILENO) < 0) _exit(127);
        close(descriptors[1]);
        execl(probe, probe, store, gateway, (char *)NULL);
        _exit(127);
    }
    close(descriptors[1]);
    if (pid < 0) { close(descriptors[0]); return -1; }
    char output[8193]; size_t used = 0; ssize_t count;
    while (used < sizeof(output) - 1 && (count = read(descriptors[0], output + used, sizeof(output) - 1 - used)) > 0)
        used += (size_t)count;
    close(descriptors[0]); output[used] = 0;
    int status;
    if (waitpid(pid, &status, 0) != pid || !WIFEXITED(status) || WEXITSTATUS(status)) return -1;
    char *line = strstr(output, "IDENTITY_PROBE ");
    if (!line) return -1;
    cJSON *proof = cJSON_Parse(line + 15);
    const char *hash = cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(proof, "dev_id_sha256"));
    bool valid = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(proof, "bound_identity_loaded")) &&
        hash && !strcmp(hash, field("expected_cloud_id_sha256"));
    cJSON_Delete(proof);
    event("bound_identity_verified", valid ? 0 : -1);
    return valid ? 0 : -1;
}
#define SET_OPTION(option, ...) do { int32_t r = IotcOhSetOption(option, __VA_ARGS__); \
    if (r) { event("set_option_failed", r); return r; } } while (0)
static int start_sdk(void) {
    if (IotcOhDevInit() || IotcOhWifiEnable()) return -1;
    SET_OPTION(IOTC_OH_OPTION_SDK_CONFIG_PATH, "sdk-state");
    SET_OPTION(IOTC_OH_OPTION_SDK_MAIN_TASK_SIZE, (uint32_t)262144);
#ifdef IOTC_CONNECT_DEVICE_WATCH_DOG_SUPPORT
    SET_OPTION(IOTC_OH_OPTION_SDK_MONITOR_TASK_SIZE, (uint32_t)131072);
#endif
    SET_OPTION(IOTC_OH_OPTION_SDK_REG_EVENT_LISTENER, sdk_event);
    SET_OPTION(IOTC_OH_OPTION_DEVICE_PUT_CHAR_STATE_CALLBACK, put_state);
    SET_OPTION(IOTC_OH_OPTION_DEVICE_GET_CHAR_STATE_CALLBACK, get_state);
    SET_OPTION(IOTC_OH_OPTION_DEVICE_REPORT_ALL_CALLBACK, report_all);
    SET_OPTION(IOTC_OH_OPTION_DEVICE_GET_PINCODE_CALLBACK, get_pin);
    SET_OPTION(IOTC_OH_OPTION_DEVICE_GET_AC_KEY_CALLBACK, get_key);
    SET_OPTION(IOTC_OH_OPTION_DEVICE_DATA_FREE_CALLBACK, free);
    SET_OPTION(IOTC_OH_OPTION_DEVICE_REBOOT_CALLBACK, reboot);
    SET_OPTION(IOTC_OH_OPTION_WIFI_GET_CERT_CALLBACK, get_ca);
    SET_OPTION(IOTC_OH_OPTION_WIFI_ENABLE_FUNC_CALLBACK, wifi_enable);
    const cJSON *protocol = cJSON_GetObjectItemCaseSensitive(config, "protType");
    int protocol_type = cJSON_IsNumber(protocol) ? protocol->valueint : IOTC_PROT_TYPE_ETHERNET;
    if (protocol_type != IOTC_PROT_TYPE_WIFI && protocol_type != IOTC_PROT_TYPE_ETHERNET) return -1;
    IotcDeviceInfo device = {
        .sn = field("sn"), .prodId = field("prodId"), .subProdId = "", .model = field("model"),
        .devTypeId = field("devTypeId"), .devTypeName = field("devTypeName"), .manuId = field("manuId"),
        .manuName = field("manuName"), .devName = field("devName"), .fwv = "0.1.0", .hwv = "0.1.0", .swv = "0.1.0",
        .uniqueId = "", .customData = "", .protType = protocol_type,
    };
    SET_OPTION(IOTC_OH_OPTION_DEVICE_DEV_INFO, &device);
    /* ZG37 profile 不包含 switch；只声明已实现且可读取真实 IP 的 netInfo。 */
    if (pin && !strcmp(field("prodId"), "ZG37")) sdk_services = gateway_services;
    SET_OPTION(IOTC_OH_OPTION_DEVICE_SVC_INFO, sdk_services, (uint32_t)1);
    SET_OPTION(IOTC_OH_OPTION_WIFI_NETCFG_MODE, IOTC_NET_CONFIG_MODE_NONE);
    int32_t r = IotcOhMain(); event("sdk_start", r);
    sdk_started = r == 0;
    return r;
}
int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IOLBF, 0);
    if (argc < 3) { fprintf(stderr, "用法: bridge_poc PRIVATE_RUNTIME_DIR --local-read|--local-write JSON|--lan-probe CONFIG|--live CONFIG\n"); return 2; }
    bool write_mode = !strcmp(argv[2], "--local-write"), read_mode = !strcmp(argv[2], "--local-read");
    bool probe = !strcmp(argv[2], "--lan-probe"), live = !strcmp(argv[2], "--live");
    if ((!write_mode && !read_mode && !probe && !live) || argc != (read_mode ? 3 : 4)) return 2;
    if ((probe || live) && load_config(argv[3], live)) return 2;
    if (mkdir(argv[1], 0700) && errno != EEXIST) { perror("runtime mkdir"); return 2; }
    struct stat st;
    if (lstat(argv[1], &st) || !S_ISDIR(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 077) || chdir(argv[1])) {
        fprintf(stderr, "运行目录必须独立、归当前用户所有且权限为 0700\n"); return 2;
    }
    int lock = open("process.lock", O_CREAT | O_RDWR | O_NOFOLLOW, 0600);
    if (lock < 0 || flock(lock, LOCK_EX | LOCK_NB)) { fprintf(stderr, "实验目录被占用\n"); return 2; }
    if (live && verify_bound(argv[0], argv[3])) return 2;
    const cJSON *enable_child = cJSON_GetObjectItemCaseSensitive(config, "enable_virtual_child");
    if (enable_child && (!cJSON_IsBool(enable_child) || !live || strcmp(field("prodId"), "ZG37"))) return 2;
    if (PocChildInit(live ? field("sn") : NULL, cJSON_IsTrue(enable_child))) return 2;
    const cJSON *enable_ha = cJSON_GetObjectItemCaseSensitive(config, "enable_ha_children");
    if (enable_ha && (!cJSON_IsBool(enable_ha) || !live || strcmp(field("prodId"), "ZG37"))) return 2;
    if (PocHaInit(live ? field("sn") : NULL, cJSON_IsTrue(enable_ha))) return 2;
    char *saved = read_file("switch.json", 128);
    if (saved) {
        if (validate_switch(saved, (uint32_t)strlen(saved), &switch_on)) { free(saved); return 2; }
        free(saved); event("local_state_restored", switch_on);
    } else if (access("switch.json", F_OK) == 0) { fprintf(stderr, "状态文件不可读\n"); return 2; }
    IotcCharState state = {.svcId = "switch"};
    if (write_mode) {
        state.data = argv[3]; state.len = (uint32_t)strlen(argv[3]);
        return put_state(&state, 1) ? 2 : 0;
    }
    if (read_mode) {
        char *out = NULL; uint32_t len = 0;
        if (get_state(&state, &out, &len, 1)) return 2;
        printf("POC {\"event\":\"local_read_result\",\"state\":%s}\n", out); free(out); return 0;
    }
    char ip[64]; if (IotcGetLocalIp(ip, sizeof(ip))) { fprintf(stderr, "POC_INTERFACE 缺失或没有有效 IPv4 地址\n"); return 2; }
    if (getenv("POC_LAN_PORT")) {
        uint16_t port = PocGetLanPort(0);
        if (!port) { fprintf(stderr, "POC_LAN_PORT 必须是 1024–65535\n"); return 2; }
        event("lan_port_override", port);
    }
    printf("POC {\"event\":\"network_ip\",\"ip\":\"%s\"}\n", ip);
    if (probe) event("probe_credentials_unavailable", 1);
    signal(SIGTERM, signal_quit); signal(SIGINT, signal_quit);
    if (start_sdk()) return 1;
    /* 不卸载库/释放回调；整进程退出交由 OS 回收上游 detached 线程。 */
    while (!quitting) {
        sleep(1);
        int current;
        pthread_mutex_lock(&state_mutex);
        current = switch_on;
        pthread_mutex_unlock(&state_mutex);
        static int reported = -1;
        if (current != reported && sdk_started) { report_all(); reported = current; }
    }
    event("process_exit_no_factory_reset", 0);
    return 0;
}
