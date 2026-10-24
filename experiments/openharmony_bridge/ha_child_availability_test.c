/* 回环后端 + 云接口替身，测试真实子设备状态机；不启动 SDK 或连接云端。 */
#define _POSIX_C_SOURCE 200809L
#define GetM2mCloudCtx TestCloudContext
#define M2mCloudSendRequest TestCloudSend
#define M2mCloudReportMessage TestCloudReport
#define SchedTimerAdd TestTimerAdd
#include "ha_children.c"

static M2mCloudContext context;
static CoapClientRespHandler awaiting;
static int status_requests, bind_requests, sync_requests, reports;
static char status_value[16];

M2mCloudContext *TestCloudContext(void) { return &context; }
int32_t TestTimerAdd(EventSourceTimerType type, EventSourceTimerCallback callback, uint32_t delay, void *data) {
    (void)type; (void)callback; (void)delay; (void)data;
    return 1;
}
int32_t TestCloudSend(M2mCloudContext *ctx, CoapClientRespHandler handler,
    M2mBuildRequest builder, const CloudOption *option) {
    if (awaiting) return -1;
    cJSON *payload = builder(ctx);
    if (!payload) return -1;
    if (!strcmp(option->uri[1], "statusSync")) {
        const char *value = string_field(cJSON_GetArrayItem(payload, 0), "status");
        if (!value) { cJSON_Delete(payload); return -1; }
        snprintf(status_value, sizeof(status_value), "%s", value);
        ++status_requests;
    } else if (!strcmp(option->uri[1], "devInfoSync")) ++sync_requests;
    else ++bind_requests;
    cJSON_Delete(payload); awaiting = handler;
    return 0;
}
int32_t TestCloudReport(const IotcJson *array, M2mCloudContext *ctx) {
    (void)ctx;
    if (!cJSON_IsArray(array) || !cJSON_GetArraySize(array)) return -1;
    ++reports;
    return 0;
}
int main(int argc, char **argv) {
    if (argc != 2 || chdir(argv[1]) || PocHaInit("OHPOC-local-ha-smoke", true)) return 2;
    context.stateManager.fsmCtx = calloc(1, sizeof(*context.stateManager.fsmCtx));
    if (!context.stateManager.fsmCtx) return 2;
    context.stateManager.fsmCtx->cur = M2M_CLOUD_FSM_STATE_ONLINE;
    PocHaCloudOnline();
    char line[128], command[32], key[32];
    while (fgets(line, sizeof(line), stdin)) {
        if (sscanf(line, "%31s %31s", command, key) != 2) return 2;
        current = NULL;
        for (size_t i = 0; i < child_count; ++i)
            if (!strcmp(children[i].key, key)) current = &children[i];
        if (!current) return 2;
        int result = 0;
        if (!strcmp(command, "step")) {
            ha_child.reported_at = 0; tick_child(0, NULL);
        } else if (!strcmp(command, "retry")) {
            ha_child.retry_at = 0; tick_child(0, NULL);
        } else if (!strcmp(command, "ack") || !strcmp(command, "fail") || !strcmp(command, "timeout")) {
            if (!awaiting) return 2;
            const char *body = !strcmp(command, "fail") ? "{\"errcode\":1}" : "{\"errcode\":0}";
            CoapPacket packet = {0}; packet.header.code = 69;
            packet.payload.data = (uint8_t *)body; packet.payload.len = strlen(body);
            CoapClientRespHandler handler = awaiting; awaiting = NULL;
            handler(&packet, NULL, NULL, !strcmp(command, "timeout"));
        } else if (!strcmp(command, "read") || !strcmp(command, "write")) {
            IotcCharState state = {.devId=ha_child.dev_id, .svcId="switch", .data="{\"on\":1}", .len=8};
            char *out = NULL; uint32_t len = 0;
            result = !strcmp(command, "write") ? PocHaPut(&state) : PocHaGet(&state, &out, &len);
            free(out);
        } else if (!strcmp(command, "reconnect")) {
            PocHaCloudOffline(); PocHaCloudOnline();
        } else return 2;
        printf("TEST {\"result\":%d,\"phase\":%d,\"available\":%d,\"announced\":%d,"
            "\"pending\":%d,\"cached\":%d,\"status_requests\":%d,\"status\":\"%s\","
            "\"bind_requests\":%d,\"sync_requests\":%d,\"reports\":%d}\n",
            result, ha_child.phase, ha_child.available, ha_child.announced, ha_child.pending,
            ha_child.reported != NULL, status_requests, status_value, bind_requests, sync_requests, reports);
        fflush(stdout);
    }
    return ferror(stdin) ? 2 : 0;
}
