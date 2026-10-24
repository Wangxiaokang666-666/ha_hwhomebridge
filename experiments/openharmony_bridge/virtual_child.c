/* 独立实验网关的单个虚拟子设备。注册/同步经真实 TLS 云连接，无 HA 依赖。 */
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "cJSON.h"
#include "mbedtls/sha256.h"
#include "virtual_child.h"
#include "m2m_cloud_send.h"
#include "m2m_cloud_report.h"
#include "sched_timer.h"
#include "utils_bit_map.h"

enum ChildPhase { CHILD_BIND = 1, CHILD_SYNC, CHILD_ONLINE, CHILD_READY };
static struct {
    bool enabled, connected, pending, dirty;
    char parent_sn[40], sn[40], udid[65], dev_id[129], mac[18];
    int on, attempts, timer;
    enum ChildPhase phase;
} child = {.timer = -1};

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
        cJSON_AddStringToObject(json, "parent_sn", child.parent_sn) &&
        cJSON_AddStringToObject(json, "sn", child.sn) && cJSON_AddStringToObject(json, "udid", child.udid) &&
        cJSON_AddStringToObject(json, "devId", device_id) && cJSON_AddNumberToObject(json, "on", on);
    char *data = ok ? cJSON_PrintUnformatted(json) : NULL;
    cJSON_Delete(json);
    if (!data) return -1;
    int fd = open("virtual-child.tmp", O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW, 0600);
    int ret = fd < 0 ? -1 : 0;
    if (!ret && (fchmod(fd, 0600) || write_all(fd, data, strlen(data)) || fsync(fd))) ret = -1;
    if (fd >= 0 && close(fd)) ret = -1;
    free(data);
    if (ret || rename("virtual-child.tmp", "virtual-child.json")) return -1;
    fd = open(".", O_RDONLY | O_DIRECTORY);
    if (fd < 0) return -1;
    ret = fsync(fd); close(fd);
    return ret;
}
static int restore(void) {
    int fd = open("virtual-child.json", O_RDONLY | O_NOFOLLOW);
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
        parent && !strcmp(parent, child.parent_sn) && sn && !strcmp(sn, child.sn) &&
        udid && !strcmp(udid, child.udid) && valid_id(dev_id, true) && cJSON_IsNumber(on) &&
        (on->valuedouble == 0 || on->valuedouble == 1);
    if (ok) { strcpy(child.dev_id, dev_id); child.on = on->valueint; }
    cJSON_Delete(json);
    if (ok) note("child_state_restored", child.on);
    return ok ? 0 : -1;
}
int PocChildInit(const char *parent_sn, bool enabled) {
    child.enabled = enabled;
    if (!enabled) return 0;
    if (!parent_sn || strncmp(parent_sn, "OHPOC-", 6) || strlen(parent_sn) >= sizeof(child.parent_sn)) return -1;
    strcpy(child.parent_sn, parent_sn);
    char input[128], hash[65];
    snprintf(input, sizeof(input), "%s#virtual-switch-v1", parent_sn);
    if (hash_hex(input, hash)) return -1;
    snprintf(child.sn, sizeof(child.sn), "OHPOC-CSW-%.24s", hash);
    snprintf(child.mac, sizeof(child.mac), "02:%.2s:%.2s:%.2s:%.2s:%.2s", hash, hash + 2, hash + 4, hash + 6, hash + 8);
    snprintf(input, sizeof(input), "OpenHarmonyOHPOC-SW%s", child.sn);
    if (hash_hex(input, child.udid)) return -1;
    return restore();
}
bool PocChildEnabled(void) { return child.enabled; }
bool PocChildMatches(const char *device_id) {
    return child.enabled && device_id && child.dev_id[0] && !strcmp(device_id, child.dev_id);
}
bool PocChildRootMatches(const char *device_id) {
    if (!device_id || !device_id[0] || !strcmp(device_id, "0")) return true;
    const M2mCloudContext *ctx = GetM2mCloudCtx();
    return ctx && !strcmp(device_id, ctx->authInfo.loginInfo.devId);
}
int PocChildGet(const IotcCharState *state, char **out, uint32_t *len) {
    if (!state || !out || !len || !PocChildMatches(state->devId) || !state->svcId || strcmp(state->svcId, "switch")) return -1;
    char data[16]; snprintf(data, sizeof(data), "{\"on\":%d}", child.on);
    *out = strdup(data);
    if (!*out) return -1;
    *len = (uint32_t)strlen(*out); note("child_service_read", child.on);
    return 0;
}
int PocChildPut(const IotcCharState *state) {
    if (!state || !PocChildMatches(state->devId) || !state->svcId || strcmp(state->svcId, "switch") ||
        !state->data || !state->len || state->len > 128 || memchr(state->data, 0, state->len)) return -1;
    char data[129]; memcpy(data, state->data, state->len); data[state->len] = 0;
    cJSON *json = cJSON_ParseWithOpts(data, NULL, true);
    const cJSON *on = cJSON_GetObjectItemCaseSensitive(json, "on");
    int value = cJSON_IsNumber(on) ? on->valueint : -1;
    bool ok = cJSON_IsObject(json) && cJSON_GetArraySize(json) == 1 && cJSON_IsNumber(on) &&
        (on->valuedouble == 0 || on->valuedouble == 1);
    cJSON_Delete(json);
    if (!ok || persist(child.dev_id, value)) { note("child_service_write_rejected", -1); return -1; }
    child.on = value; child.dirty = true; note("child_service_write", child.on);
    return 0;
}
static cJSON *device_info(void) {
    cJSON *info = cJSON_CreateObject();
    if (!info) return NULL;
    bool ok = cJSON_AddStringToObject(info, "sn", child.sn) &&
        cJSON_AddStringToObject(info, "udid", child.udid) && cJSON_AddStringToObject(info, "subProdId", "00") &&
        cJSON_AddStringToObject(info, "model", "OHPOC-SW") && cJSON_AddStringToObject(info, "devType", "119") &&
        cJSON_AddStringToObject(info, "manu", "002") && cJSON_AddStringToObject(info, "prodId", "9MU7") &&
        cJSON_AddStringToObject(info, "mac", child.mac) && cJSON_AddStringToObject(info, "hiv", "1.0") &&
        cJSON_AddStringToObject(info, "fwv", "0.1.0") && cJSON_AddStringToObject(info, "hwv", "0.1.0") &&
        cJSON_AddStringToObject(info, "swv", "0.1.0") && cJSON_AddNumberToObject(info, "protType", 6) &&
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
    cJSON *services = cJSON_AddArrayToObject(item, "services"), *service = cJSON_CreateObject();
    if (!services || !service) { cJSON_Delete(service); cJSON_Delete(array); return NULL; }
    cJSON_AddItemToArray(services, service);
    if (!cJSON_AddStringToObject(item, "devId", child.dev_id) ||
        !cJSON_AddStringToObject(service, "st", "switch") || !cJSON_AddStringToObject(service, "sid", "switch")) {
        cJSON_Delete(array); return NULL;
    }
    return array;
}
static IotcJson *build_online(M2mCloudContext *ctx) {
    cJSON *array = cJSON_CreateArray(), *item = cJSON_CreateObject();
    if (!array || !item) { cJSON_Delete(array); cJSON_Delete(item); return NULL; }
    cJSON_AddItemToArray(array, item);
    if (!cJSON_AddStringToObject(item, "devId", child.dev_id) ||
        !cJSON_AddStringToObject(item, "gatewayId", ctx->authInfo.loginInfo.devId) ||
        !cJSON_AddStringToObject(item, "status", "online")) { cJSON_Delete(array); return NULL; }
    return array;
}
static void response(enum ChildPhase phase, const CoapPacket *packet, bool timeout) {
    if (!child.connected || child.phase != phase || !child.pending) return;
    child.pending = false;
    if (timeout || !packet || !packet->payload.data || !packet->payload.len || packet->payload.len > 65536) {
        note("child_cloud_response_timeout_or_invalid", phase); return;
    }
    cJSON *json = cJSON_ParseWithLength((const char *)packet->payload.data, packet->payload.len);
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(json, "errcode");
    const cJSON *failures = cJSON_GetObjectItemCaseSensitive(json, "failureList");
    int code = cJSON_IsNumber(error) ? error->valueint : -1;
    note("child_cloud_response_phase", phase); note("child_cloud_response_code", code);
    bool ok = packet->header.code >= 64 && packet->header.code < 96 && cJSON_IsNumber(error) &&
        error->valuedouble == 0 && (!failures || (cJSON_IsArray(failures) && !cJSON_GetArraySize(failures)));
    if (ok && phase == CHILD_BIND) {
        const cJSON *success = cJSON_GetObjectItemCaseSensitive(json, "successList");
        const cJSON *item = cJSON_GetArrayItem(success, 0);
        const char *sn = string_field(item, "sn"), *id = string_field(item, "devId");
        ok = cJSON_IsArray(success) && cJSON_GetArraySize(success) == 1 && sn && !strcmp(sn, child.sn) && valid_id(id, false);
        if (ok) {
            ok = persist(id, child.on) == 0;
            if (ok) strcpy(child.dev_id, id);
        }
    }
    cJSON_Delete(json);
    if (!ok) { note("child_cloud_step_failed", phase); return; }
    child.attempts = 0; child.phase = phase + 1;
    note(phase == CHILD_BIND ? "child_registered" : phase == CHILD_SYNC ? "child_info_synced" : "child_online", 0);
    if (child.phase == CHILD_READY) child.dirty = true;
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
static void tick(int32_t id, void *data) {
    (void)id; (void)data;
    M2mCloudContext *ctx = GetM2mCloudCtx();
    if (!child.connected || !ctx->stateManager.fsmCtx || ctx->stateManager.fsmCtx->cur != M2M_CLOUD_FSM_STATE_ONLINE) return;
    if (child.phase == CHILD_READY) {
        if (!child.dirty) return;
        cJSON *array = cJSON_CreateArray(), *item = cJSON_CreateObject();
        if (!array || !item) { cJSON_Delete(array); cJSON_Delete(item); return; }
        cJSON_AddItemToArray(array, item);
        cJSON *state = cJSON_AddObjectToObject(item, "data");
        if (!state || !cJSON_AddStringToObject(item, "devId", child.dev_id) ||
            !cJSON_AddStringToObject(item, "sid", "switch") || !cJSON_AddNumberToObject(state, "on", child.on)) {
            cJSON_Delete(array); return;
        }
        int ret = M2mCloudReportMessage(array, ctx); cJSON_Delete(array);
        note("child_report_queued", ret);
        if (!ret) child.dirty = false;
        return;
    }
    if (child.pending || child.attempts >= 3) return;
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
    child.pending = true; ++child.attempts;
    int step = child.phase - 1;
    int ret = M2mCloudSendRequest(ctx, handlers[step], builders[step], &options[step]);
    note("child_cloud_request_phase", child.phase); note("child_cloud_request_queued", ret);
    if (ret) child.pending = false;
}
void PocChildCloudOnline(void) {
    if (!child.enabled) return;
    child.connected = true; child.pending = false; child.attempts = 0;
    child.phase = child.dev_id[0] ? CHILD_SYNC : CHILD_BIND;
    if (child.timer < 0) child.timer = SchedTimerAdd(EVENT_SOURCE_TIMER_TYPE_REPEAT, tick, 2000, NULL);
    note("child_timer", child.timer);
}
void PocChildCloudOffline(void) { child.connected = false; child.pending = false; }
