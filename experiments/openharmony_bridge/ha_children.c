/* 第二阶段：实验网关的 HA 子设备；原虚拟开关保持独立。 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/stat.h>
#include <unistd.h>
#include "cJSON.h"
#include "mbedtls/sha256.h"
#include "ha_children.h"
#include "m2m_cloud_send.h"
#include "m2m_cloud_report.h"
#include "sched_timer.h"
#include "utils_bit_map.h"

enum ChildPhase { CHILD_BIND = 1, CHILD_SYNC, CHILD_ONLINE, CHILD_READY };
#include <arpa/inet.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
/* 身份文件沿用 v1 的 on=0 保留字段；真实状态始终由 HA 读取，不从此字段恢复。 */
struct HaChild {
    bool enabled, connected, pending, dirty, available, sent_available;
    int announced;
    time_t retry_at;
    char parent_sn[40], sn[40], udid[65], dev_id[129], mac[18];
    char key[32], state_path[80], temp_path[80];
    const cJSON *definition;
    char *reported;
    time_t reported_at;
    int on, attempts, timer;
    enum ChildPhase phase;
};
static struct HaChild children[64], *current;
static size_t child_count;
static int ha_timer = -1;
static cJSON *definitions;
#define ha_child (*current)

static void note(const char *event, int result) {
    printf("POC {\"event\":\"%s\",\"result\":%d}\n", event, result);
    fflush(stdout);
}
static int hash_hex(const char *input, char out[65]) {
    unsigned char hash[32];
    if (mbedtls_sha256_ret((const unsigned char *)input, strlen(input), hash, 0)) return -1;
    for (size_t i = 0; i < sizeof(hash); ++i) snprintf(out + 2 * i, 3, "%02x", hash[i]);
    return 0;
}
static const char *string_field(const cJSON *object, const char *key) {
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, key);
    return cJSON_IsString(item) ? item->valuestring : NULL;
}
static bool valid_id(const char *id, bool empty) {
    if (!id || strlen(id) > 128 || (!empty && !id[0])) return false;
    return strspn(id, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:") == strlen(id);
}
static int write_all(int fd, const char *data, size_t len) {
    while (len) {
        ssize_t count = write(fd, data, len);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) return -1;
        data += count; len -= (size_t)count;
    }
    return 0;
}
static int persist(const char *device_id, int on) {
    cJSON *json = cJSON_CreateObject();
    if (!json) return -1;
    bool ok = cJSON_AddNumberToObject(json, "version", 1) &&
        cJSON_AddStringToObject(json, "parent_sn", ha_child.parent_sn) &&
        cJSON_AddStringToObject(json, "sn", ha_child.sn) && cJSON_AddStringToObject(json, "udid", ha_child.udid) &&
        cJSON_AddStringToObject(json, "devId", device_id) && cJSON_AddNumberToObject(json, "on", on);
    char *data = ok ? cJSON_PrintUnformatted(json) : NULL;
    cJSON_Delete(json);
    if (!data) return -1;
    int fd = open(ha_child.temp_path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    int ret = fd < 0 ? -1 : 0;
    if (!ret && (fchmod(fd, 0600) || write_all(fd, data, strlen(data)) || fsync(fd))) ret = -1;
    if (fd >= 0 && close(fd)) ret = -1;
    free(data);
    if (ret || rename(ha_child.temp_path, ha_child.state_path)) return -1;
    fd = open(".", O_RDONLY | O_DIRECTORY);
    if (fd < 0) return -1;
    ret = fsync(fd); close(fd);
    return ret;
}
static int restore(void) {
    int fd = open(ha_child.state_path, O_RDONLY | O_NOFOLLOW);
    if (fd < 0) return errno == ENOENT ? persist("", 0) : -1;
    struct stat st;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 077) ||
        st.st_size <= 0 || st.st_size > 2048) { close(fd); return -1; }
    char data[2049]; size_t size = 0;
    while (size < (size_t)st.st_size) {
        ssize_t count = read(fd, data + size, (size_t)st.st_size - size);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) { close(fd); return -1; }
        size += (size_t)count;
    }
    close(fd); data[size] = 0;
    if (memchr(data, 0, size)) return -1;
    cJSON *json = cJSON_ParseWithOpts(data, NULL, true);
    const char *parent = string_field(json, "parent_sn"), *sn = string_field(json, "sn");
    const char *udid = string_field(json, "udid"), *dev_id = string_field(json, "devId");
    const cJSON *on = cJSON_GetObjectItemCaseSensitive(json, "on");
    const cJSON *version = cJSON_GetObjectItemCaseSensitive(json, "version");
    bool ok = cJSON_IsObject(json) && cJSON_GetArraySize(json) == 6 &&
        cJSON_IsNumber(version) && version->valuedouble == 1 &&
        parent && !strcmp(parent, ha_child.parent_sn) && sn && !strcmp(sn, ha_child.sn) &&
        udid && !strcmp(udid, ha_child.udid) && valid_id(dev_id, true) && cJSON_IsNumber(on) &&
        (on->valuedouble == 0 || on->valuedouble == 1);
    if (ok) { strcpy(ha_child.dev_id, dev_id); ha_child.on = on->valueint; }
    cJSON_Delete(json);
    if (ok) note("ha_identity_restored", 0);
    return ok ? 0 : -1;
}
static struct HaChild *find_child(const char *device_id) {
    if (device_id && device_id[0]) for (size_t i = 0; i < child_count; ++i)
        if (children[i].dev_id[0] && !strcmp(device_id, children[i].dev_id)) return &children[i];
    return NULL;
}
int PocHaInit(const char *parent_sn, bool enabled) {
    if (!enabled) return 0;
    if (!parent_sn || !parent_sn[0] || strlen(parent_sn) >= 40 ||
        strspn(parent_sn, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") != strlen(parent_sn)) return -1;
    int fd = open("ha-children.local.json", O_RDONLY | O_NOFOLLOW);
    struct stat st;
    if (fd < 0) return -1;
    if (fstat(fd, &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 077) ||
        st.st_size <= 0 || st.st_size > 131072) { close(fd); return -1; }
    char data[131073]; ssize_t count = read(fd, data, sizeof(data) - 1); close(fd);
    if (count != st.st_size || memchr(data, 0, (size_t)count)) return -1;
    data[count] = 0; definitions = cJSON_ParseWithOpts(data, NULL, true);
    if (!cJSON_IsArray(definitions) || cJSON_GetArraySize(definitions) < 1 || cJSON_GetArraySize(definitions) > 64) return -1;
    child_count = (size_t)cJSON_GetArraySize(definitions);
    for (size_t i = 0; i < child_count; ++i) {
        current = &children[i]; ha_child.definition = cJSON_GetArrayItem(definitions, (int)i);
        const char *key = string_field(ha_child.definition, "key");
        if (!key || !key[0] || strlen(key) >= sizeof(ha_child.key) ||
            strspn(key, "abcdefghijklmnopqrstuvwxyz0123456789-") != strlen(key) ||
            !string_field(ha_child.definition, "pid") || !string_field(ha_child.definition, "devType") ||
            !string_field(ha_child.definition, "manu") || !cJSON_IsObject(cJSON_GetObjectItemCaseSensitive(ha_child.definition, "services"))) return -1;
        for (size_t j = 0; j < i; ++j) if (!strcmp(children[j].key, key)) return -1;
        strcpy(ha_child.key, key); strcpy(ha_child.parent_sn, parent_sn); ha_child.enabled = true; ha_child.announced = -1;
        snprintf(ha_child.state_path, sizeof(ha_child.state_path), "ha-child-%s.json", key);
        snprintf(ha_child.temp_path, sizeof(ha_child.temp_path), "ha-child-%s.tmp", key);
        char input[128], hash[65]; snprintf(input, sizeof(input), "%s#ha-%s-v1", parent_sn, key);
        if (hash_hex(input, hash)) return -1;
        snprintf(ha_child.sn, sizeof(ha_child.sn), "OHPOC-HA-%.24s", hash);
        /* 单引擎实验可保留公共 VirtualDevice 已有 SN；默认实验身份算法不变。 */
        const cJSON *sn_item = cJSON_GetObjectItemCaseSensitive(ha_child.definition, "sn");
        if (sn_item && !cJSON_IsString(sn_item)) return -1;
        const char *saved_sn = string_field(ha_child.definition, "sn");
        if (saved_sn) {
            if (!valid_id(saved_sn, false) || strlen(saved_sn) >= sizeof(ha_child.sn)) return -1;
            strcpy(ha_child.sn, saved_sn);
        }
        for (size_t j = 0; j < i; ++j)
            if (!strcmp(children[j].sn, ha_child.sn)) return -1;
        snprintf(ha_child.mac, sizeof(ha_child.mac), "02:%.2s:%.2s:%.2s:%.2s:%.2s", hash, hash+2, hash+4, hash+6, hash+8);
        snprintf(input, sizeof(input), "OpenHarmonyHA%s%s", key, ha_child.sn);
        if (hash_hex(input, ha_child.udid) || restore()) return -1;
        if (cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(ha_child.definition, "existing_cloud_only")) &&
            !ha_child.dev_id[0]) return -1;
        for (size_t j = 0; j < i; ++j)
            if (ha_child.dev_id[0] && !strcmp(children[j].dev_id, ha_child.dev_id)) return -1;
    }
    return 0;
}
bool PocHaEnabled(void) { return child_count > 0; }
bool PocHaMatches(const char *device_id) { return find_child(device_id) != NULL; }
/* 不记录载荷或身份；请求号只在本进程内递增，用于和回环服务日志对照。 */
struct BackendTrace {
    unsigned long id;
    struct timespec started;
    const char *reason;
    int system_error;
};
static unsigned long backend_sequence;
/* 回环服务总是返回 Content-Length。读满一帧即可结束，不能等连接关闭来确认。 */
static int backend_frame_size(char *response, size_t size, size_t capacity, size_t *total) {
    response[size] = 0;
    char *end = strstr(response, "\r\n\r\n");
    if (!end) return 0;
    char *line = strstr(response, "\r\n");
    if (!line || strncmp(response, "HTTP/1.0 200 ", 13)) return -1;
    bool found = false;
    size_t length = 0;
    for (line += 2; line < end; ) {
        char *next = strstr(line, "\r\n");
        if (!next || next > end) return -1;
        if (!strncasecmp(line, "Transfer-Encoding:", 18)) return -1;
        if (!strncasecmp(line, "Content-Length:", 15)) {
            if (found) return -1;
            found = true;
            char *value = line + 15;
            while (value < next && (*value == ' ' || *value == '\t')) ++value;
            if (value == next) return -1;
            for (; value < next; ++value) {
                if (*value < '0' || *value > '9' || length > capacity / 10) return -1;
                length = length * 10 + (size_t)(*value - '0');
                if (length >= capacity) return -1;
            }
        }
        line = next + 2;
    }
    size_t header = (size_t)(end + 4 - response);
    if (!found || !length || length >= capacity - header) return -1;
    *total = header + length;
    return 1;
}
static void backend_trace(const char *sid, const struct BackendTrace *trace, const cJSON *json) {
    if (!sid && (!trace->reason || cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(json, "available")))) return;
    struct timespec now;
    clock_gettime(CLOCK_MONOTONIC, &now);
    long elapsed = (now.tv_sec - trace->started.tv_sec) * 1000 +
        (now.tv_nsec - trace->started.tv_nsec) / 1000000;
    const char *error = string_field(json, "error");
    const char *safe_error = "none";
    const char *allowed[] = {"TimeoutError", "SourceUnavailable", "ValueError", "RuntimeError", "KeyError", "OSError"};
    if (error) {
        safe_error = "BackendError";
        for (size_t i = 0; i < sizeof(allowed) / sizeof(allowed[0]); ++i)
            if (!strcmp(error, allowed[i])) safe_error = allowed[i];
    }
    printf("HA {\"event\":\"backend_result\",\"request_id\":%lu,\"operation\":\"%s\","
        "\"reason\":\"%s\",\"error_type\":\"%s\",\"errno\":%d,\"elapsed_ms\":%ld}\n",
        trace->id, sid ? "write" : "read", trace->reason ? trace->reason : "accepted",
        safe_error, trace->system_error, elapsed);
    fflush(stdout);
}
/* Linux loopback backend; the standalone deployment connects directly to HA. */
static cJSON *backend_request(const char *sid, const cJSON *value, struct BackendTrace *trace) {
    trace->id = ++backend_sequence;
    clock_gettime(CLOCK_MONOTONIC, &trace->started);
    trace->reason = "request_build_failed";
    cJSON *request = cJSON_CreateObject();
    if (!request) return NULL;
    cJSON_AddStringToObject(request, "key", ha_child.key);
    if (sid) {
        cJSON_AddStringToObject(request, "sid", sid);
        cJSON_AddItemToObject(request, "data", cJSON_Duplicate(value, true));
    }
    char *body = cJSON_PrintUnformatted(request); cJSON_Delete(request);
    if (!body) return NULL;
    unsigned short port = 18089;
    const char *port_env = getenv("POC_HA_PROXY_PORT");
    if (port_env) {
        char *end; long parsed = strtol(port_env, &end, 10);
        if (!port_env[0] || *end || parsed < 1024 || parsed > 65535) {free(body); return NULL;}
        port = (unsigned short)parsed;
    }
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    struct timeval timeout = {.tv_sec=12};
    struct sockaddr_in address = {.sin_family=AF_INET, .sin_port=htons(port), .sin_addr.s_addr=htonl(INADDR_LOOPBACK)};
    char message[10000];
    int length = snprintf(message, sizeof(message), "POST /bridge HTTP/1.0\r\nHost: localhost\r\nContent-Type: application/json\r\nX-HWHB-Request-Id: %lu\r\nContent-Length: %zu\r\n\r\n%s", trace->id, strlen(body), body);
    free(body);
    if (fd < 0 || length < 0 || (size_t)length >= sizeof(message)) { if(fd >= 0) close(fd); return NULL; }
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout));
    if (connect(fd, (struct sockaddr *)&address, sizeof(address))) {
        trace->reason = "connect_failed"; trace->system_error = errno; close(fd); return NULL;
    }
    size_t sent = 0;
    while (sent < (size_t)length) {
        ssize_t n = send(fd, message + sent, (size_t)length - sent, MSG_NOSIGNAL);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) {trace->reason = "send_failed"; trace->system_error = errno; close(fd); return NULL;}
        sent += (size_t)n;
    }
    char response[16385]; size_t pos = 0, expected = 0;
    while (pos < sizeof(response)-1) {
        ssize_t n = read(fd, response+pos, sizeof(response)-1-pos);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) {
            trace->reason = errno == EAGAIN || errno == EWOULDBLOCK ? "receive_timeout" : "receive_failed";
            trace->system_error = errno; close(fd); return NULL;
        }
        if (!n) break;
        pos += (size_t)n;
        int framed = backend_frame_size(response, pos, sizeof(response), &expected);
        if (framed < 0 || (framed && pos > expected)) {
            trace->reason = "invalid_http"; close(fd); return NULL;
        }
        if (framed && pos == expected) break;
    }
    close(fd); response[pos] = 0;
    trace->reason = "truncated_http";
    if (!expected || pos != expected) return NULL;
    char *payload = strstr(response, "\r\n\r\n");
    if (!payload || strncmp(response, "HTTP/1.0 200 ", 13)) return NULL;
    cJSON *json = cJSON_ParseWithOpts(payload+4, NULL, true);
    trace->reason = json ? NULL : "invalid_json";
    return json;
}
static void source_state(bool available) {
    ha_child.available = available;
    if (!available) {
        free(ha_child.reported); ha_child.reported = NULL;
        ha_child.dirty = true;
    }
}
static cJSON *ha_request(const char *sid, const cJSON *value) {
    struct BackendTrace trace = {0};
    cJSON *json = backend_request(sid, value, &trace);
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(json, "state");
    bool available = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(json, "ok")) &&
        !cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(json, "available")) && cJSON_IsObject(state);
    if (json && !available) trace.reason = "backend_rejected";
    backend_trace(sid, &trace, json);
    /* 普通写入校验失败不等于设备离线；读取失败和传输中断必须清掉旧状态。 */
    if (available || !sid || !json || cJSON_IsFalse(cJSON_GetObjectItemCaseSensitive(json, "available")))
        source_state(available);
    cJSON *result = available ? cJSON_Duplicate(state, true) : NULL;
    cJSON_Delete(json); return result;
}

static bool has_service(const char *sid) {
    return sid && cJSON_GetObjectItemCaseSensitive(cJSON_GetObjectItemCaseSensitive(ha_child.definition, "services"), sid);
}
int PocHaGet(const IotcCharState *state, char **out, uint32_t *len) {
    if (!state || !out || !len || !(current = find_child(state->devId)) || !has_service(state->svcId)) return -1;
    cJSON *snapshot = ha_request(NULL, NULL);
    cJSON *value = cJSON_GetObjectItemCaseSensitive(snapshot, state->svcId);
    *out = cJSON_IsObject(value) ? cJSON_PrintUnformatted(value) : NULL;
    cJSON_Delete(snapshot);
    if (!*out) return -1;
    *len = (uint32_t)strlen(*out); note("ha_service_read", 0); return 0;
}
int PocHaPut(const IotcCharState *state) {
    if (!state || !(current = find_child(state->devId)) || !has_service(state->svcId) ||
        !state->data || !state->len || state->len > 4096 || memchr(state->data, 0, state->len)) return -1;
    char data[4097]; memcpy(data, state->data, state->len); data[state->len] = 0;
    cJSON *value = cJSON_ParseWithOpts(data, NULL, true);
    if (!cJSON_IsObject(value)) { cJSON_Delete(value); return -1; }
    cJSON *snapshot = ha_request(state->svcId, value); cJSON_Delete(value);
    if (!snapshot) { note("ha_service_write_rejected", -1); return -1; }
    cJSON_Delete(snapshot); ha_child.dirty = true;
    printf("HA {\"key\":\"%s\",\"sid\":\"%s\",\"write_confirmed\":true}\n", ha_child.key, state->svcId);
    note("ha_service_write_confirmed", 0); return 0;
}
static cJSON *device_info(void) {
    cJSON *info = cJSON_CreateObject();
    if (!info) return NULL;
    bool ok = cJSON_AddStringToObject(info, "sn", ha_child.sn) &&
        cJSON_AddStringToObject(info, "udid", ha_child.udid) && cJSON_AddStringToObject(info, "subProdId", "00") &&
        cJSON_AddStringToObject(info, "model", string_field(ha_child.definition, "model") ? string_field(ha_child.definition, "model") : "OHPOC-HA") && cJSON_AddStringToObject(info, "devType", string_field(ha_child.definition, "devType")) &&
        cJSON_AddStringToObject(info, "manu", string_field(ha_child.definition, "manu")) && cJSON_AddStringToObject(info, "prodId", string_field(ha_child.definition, "pid")) &&
        cJSON_AddStringToObject(info, "mac", ha_child.mac) && cJSON_AddStringToObject(info, "hiv", "1.0") &&
        cJSON_AddStringToObject(info, "fwv", "0.1.0") && cJSON_AddStringToObject(info, "hwv", "0.1.0") &&
        cJSON_AddStringToObject(info, "swv", "0.1.0") && cJSON_AddNumberToObject(info, "protType",
            cJSON_IsNumber(cJSON_GetObjectItemCaseSensitive(ha_child.definition, "protType")) ? cJSON_GetObjectItemCaseSensitive(ha_child.definition, "protType")->valueint : 6) &&
        cJSON_AddNumberToObject(info, "setupType", 0);
    if (!ok) { cJSON_Delete(info); return NULL; }
    return info;
}
static IotcJson *build_bind(M2mCloudContext *ctx) {
    (void)ctx;
    cJSON *array = cJSON_CreateArray(), *item = cJSON_CreateObject(), *info = device_info();
    if (!array || !item || !info) { cJSON_Delete(array); cJSON_Delete(item); cJSON_Delete(info); return NULL; }
    cJSON_AddItemToArray(array, item); cJSON_AddItemToObject(item, "devInfo", info);
    return array;
}
static IotcJson *build_sync(M2mCloudContext *ctx) {
    cJSON *array = build_bind(ctx);
    if (!array) return NULL;
    cJSON *item = cJSON_GetArrayItem(array, 0);
    cJSON *services = cJSON_AddArrayToObject(item, "services");
    if (!services || !cJSON_AddStringToObject(item, "devId", ha_child.dev_id)) {cJSON_Delete(array); return NULL;}
    const cJSON *mapping = cJSON_GetObjectItemCaseSensitive(ha_child.definition, "services"), *entry;
    cJSON_ArrayForEach(entry, mapping) {
        cJSON *service = cJSON_CreateObject();
        if (!service) {cJSON_Delete(array); return NULL;}
        cJSON_AddItemToArray(services, service);
        if (!cJSON_IsString(entry) || !cJSON_AddStringToObject(service, "sid", entry->string) ||
            !cJSON_AddStringToObject(service, "st", entry->valuestring)) {cJSON_Delete(array); return NULL;}
    }
    return array;
}
static IotcJson *build_online(M2mCloudContext *ctx) {
    cJSON *array = cJSON_CreateArray(), *item = cJSON_CreateObject();
    if (!array || !item) { cJSON_Delete(array); cJSON_Delete(item); return NULL; }
    cJSON_AddItemToArray(array, item);
    if (!cJSON_AddStringToObject(item, "devId", ha_child.dev_id) ||
        !cJSON_AddStringToObject(item, "gatewayId", ctx->authInfo.loginInfo.devId) ||
        !cJSON_AddStringToObject(item, "status", ha_child.sent_available ? "online" : "offline")) { cJSON_Delete(array); return NULL; }
    return array;
}
static void response(enum ChildPhase phase, const CoapPacket *packet, bool timeout) {
    current = NULL;
    for (size_t i=0; i<child_count; ++i) if (children[i].pending) { current=&children[i]; break; }
    if (!current || !ha_child.connected || ha_child.phase != phase) return;
    ha_child.pending = false;
    if (timeout || !packet || !packet->payload.data || !packet->payload.len || packet->payload.len > 65536) {
        note("child_cloud_response_timeout_or_invalid", phase); return;
    }
    cJSON *json = cJSON_ParseWithLength((const char *)packet->payload.data, packet->payload.len);
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(json, "errcode");
    const cJSON *failures = cJSON_GetObjectItemCaseSensitive(json, "failureList");
    int code = cJSON_IsNumber(error) ? error->valueint : -1;
    const cJSON *successes = cJSON_GetObjectItemCaseSensitive(json, "successList");
    printf("HA {\"key\":\"%s\",\"phase\":%d,\"code\":%d,\"failures\":%d,\"successes\":%d}\n",
        ha_child.key, phase, code, cJSON_GetArraySize(failures), cJSON_GetArraySize(successes));
    const cJSON *failure;
    cJSON_ArrayForEach(failure, failures) {
        const cJSON *field;
        cJSON_ArrayForEach(field, failure) {
            if (cJSON_IsNumber(field)) printf("HA failure numeric %s=%g\n", field->string, field->valuedouble);
            else printf("HA failure field %s\n", field->string);
        }
    }
    note("child_cloud_response_phase", phase); note("child_cloud_response_code", code);
    bool ok = packet->header.code >= 64 && packet->header.code < 96 && cJSON_IsNumber(error) &&
        error->valuedouble == 0 && (!failures || (cJSON_IsArray(failures) && !cJSON_GetArraySize(failures)));
    if (ok && phase == CHILD_BIND) {
        const cJSON *success = cJSON_GetObjectItemCaseSensitive(json, "successList");
        const cJSON *item = cJSON_GetArrayItem(success, 0);
        const char *sn = string_field(item, "sn"), *id = string_field(item, "devId");
        ok = cJSON_IsArray(success) && cJSON_GetArraySize(success) == 1 && sn && !strcmp(sn, ha_child.sn) && valid_id(id, false);
        if (ok) {
            ok = persist(id, ha_child.on) == 0;
            if (ok) strcpy(ha_child.dev_id, id);
        }
    }
    cJSON_Delete(json);
    if (!ok) { note("child_cloud_step_failed", phase); return; }
    ha_child.attempts = 0; ha_child.phase = phase + 1;
    if (phase == CHILD_ONLINE) ha_child.announced = ha_child.sent_available;
    const char *event = "child_registered";
    if (phase == CHILD_SYNC) event = "child_info_synced";
    if (phase == CHILD_ONLINE) {
        event = ha_child.sent_available ? "child_online" : "child_offline";
        printf("HA {\"key\":\"%s\",\"phase\":3,\"code\":0,\"failures\":0,"
            "\"accepted\":true,\"available\":%s}\n", ha_child.key, ha_child.sent_available ? "true" : "false");
    }
    note(event, 0);
    if (ha_child.phase == CHILD_READY) ha_child.dirty = true;
}
static void bind_response(const CoapPacket *packet, const SocketAddr *address, void *user, bool timeout) {
    (void)address; (void)user; response(CHILD_BIND, packet, timeout);
}
static void sync_response(const CoapPacket *packet, const SocketAddr *address, void *user, bool timeout) {
    (void)address; (void)user; response(CHILD_SYNC, packet, timeout);
}
static void online_response(const CoapPacket *packet, const SocketAddr *address, void *user, bool timeout) {
    (void)address; (void)user; response(CHILD_ONLINE, packet, timeout);
}
static void tick_child(int32_t id, void *data) {
    (void)id; (void)data;
    M2mCloudContext *ctx = GetM2mCloudCtx();
    if (!ha_child.connected || !ctx->stateManager.fsmCtx || ctx->stateManager.fsmCtx->cur != M2M_CLOUD_FSM_STATE_ONLINE) return;
    if (ha_child.phase == CHILD_READY) {
        if (!ha_child.dirty && time(NULL) - ha_child.reported_at < 5) return;
        ha_child.reported_at = time(NULL);
        cJSON *snapshot = ha_request(NULL, NULL);
        if (ha_child.announced != (int)ha_child.available) {
            cJSON_Delete(snapshot);
            ha_child.phase = CHILD_ONLINE; ha_child.attempts = 0;
            ha_child.retry_at = 0;
        } else if (!snapshot) return;
        else {
            char *serialized = cJSON_PrintUnformatted(snapshot);
            if (!serialized) {cJSON_Delete(snapshot); return;}
            if (!ha_child.dirty && ha_child.reported && !strcmp(serialized, ha_child.reported)) {
                free(serialized); cJSON_Delete(snapshot); return;
            }
            cJSON *array = cJSON_CreateArray();
            const cJSON *entry;
            cJSON_ArrayForEach(entry, snapshot) {
                if (!has_service(entry->string)) continue;
                cJSON *item = cJSON_CreateObject();
                if (!item) {cJSON_Delete(array); array=NULL; break;}
                cJSON_AddItemToArray(array, item);
                cJSON_AddStringToObject(item, "devId", ha_child.dev_id);
                cJSON_AddStringToObject(item, "sid", entry->string);
                cJSON_AddItemToObject(item, "data", cJSON_Duplicate(entry, true));
            }
            int ret = array ? M2mCloudReportMessage(array, ctx) : -1;
            cJSON_Delete(array); cJSON_Delete(snapshot); note("ha_report_queued", ret);
            if (!ret) {free(ha_child.reported); ha_child.reported = serialized; ha_child.dirty=false;} else free(serialized);
            return;
        }
    }
    if (ha_child.pending) return;
    if (ha_child.attempts >= 3) {
        if (ha_child.phase != CHILD_ONLINE || time(NULL) < ha_child.retry_at) return;
        ha_child.attempts = 0;
    }
    if (ha_child.phase == CHILD_ONLINE) {
        cJSON *snapshot = ha_request(NULL, NULL);
        cJSON_Delete(snapshot);
        ha_child.sent_available = ha_child.available;
        ha_child.retry_at = time(NULL) + 30;
    }
    static const char *bind_uri[] = {"device", "v3", "batchBind"};
    static const char *sync_uri[] = {".sys", "devInfoSync"};
    static const char *online_uri[] = {".sys", "statusSync"};
    const CloudOption options[] = {
        {.uri = bind_uri, .num = 3, .opBitMap = UTILS_BIT(CLOUD_OPTION_BIT_ACCESS_TOKEN_ID) | UTILS_BIT(CLOUD_OPTION_BIT_SEQ_NUM_ID)},
        {.uri = sync_uri, .num = 2, .opBitMap = UTILS_BIT(CLOUD_OPTION_BIT_ACCESS_TOKEN_ID) | UTILS_BIT(CLOUD_OPTION_BIT_SEQ_NUM_ID)},
        {.uri = online_uri, .num = 2, .opBitMap = UTILS_BIT(CLOUD_OPTION_BIT_ACCESS_TOKEN_ID) | UTILS_BIT(CLOUD_OPTION_BIT_SEQ_NUM_ID)},
    };
    const M2mBuildRequest builders[] = {build_bind, build_sync, build_online};
    const CoapClientRespHandler handlers[] = {bind_response, sync_response, online_response};
    ha_child.pending = true; ++ha_child.attempts;
    int step = ha_child.phase - 1;
    int ret = M2mCloudSendRequest(ctx, handlers[step], builders[step], &options[step]);
    note("child_cloud_request_phase", ha_child.phase); note("child_cloud_request_queued", ret);
    if (ret) ha_child.pending = false;
}
static void tick(int32_t id, void *data) {
    /* Serialize registration because SDK request builder has no user argument. */
    for (size_t i=0; i<child_count; ++i) if (children[i].pending) return;
    for (size_t i=0; i<child_count; ++i) {
        current=&children[i]; tick_child(id, data);
        if (ha_child.pending) break;
    }
}
void PocHaCloudOnline(void) {
    for (size_t i=0; i<child_count; ++i) {
        current=&children[i]; ha_child.connected=true; ha_child.pending=false; ha_child.attempts=0;
        ha_child.announced=-1; ha_child.retry_at=0;
        free(ha_child.reported); ha_child.reported=NULL; ha_child.dirty=true;
        /* 旧SDK离线接管保留云端已有元数据，只上线，不覆盖其UDID或重新绑定。 */
        bool imported = cJSON_IsTrue(cJSON_GetObjectItemCaseSensitive(ha_child.definition, "existing_cloud_only"));
        if (!ha_child.dev_id[0]) ha_child.phase = CHILD_BIND;
        else ha_child.phase = imported ? CHILD_ONLINE : CHILD_SYNC;
    }
    if (child_count && ha_timer < 0) ha_timer=SchedTimerAdd(EVENT_SOURCE_TIMER_TYPE_REPEAT, tick, 2000, NULL);
}
void PocHaCloudOffline(void) {
    for (size_t i=0; i<child_count; ++i) {children[i].connected=false; children[i].pending=false;}
}
