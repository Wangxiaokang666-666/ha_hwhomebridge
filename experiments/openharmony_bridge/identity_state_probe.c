/* 仅操作可丢弃测试副本；不启动SDK任务或网络，不输出凭据。 */
#include <stdio.h>
#include <stdint.h>
#include <stdbool.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>
#include "security_store.h"
#include "iotc_kv.h"
#include "config_info.h"
#include "security_key.h"
#include "dev_info.h"
#include "cJSON.h"
#include "iotc_store_key.h"
#include "mbedtls/sha256.h"

static uint8_t ac_key[48];
static int32_t get_key(uint8_t *out, uint32_t len) {
    if (!out || len < sizeof(ac_key)) return -1;
    memcpy(out, ac_key, sizeof(ac_key));
    return 0;
}
static const char *field(cJSON *root, const char *name) {
    return cJSON_GetStringValue(cJSON_GetObjectItemCaseSensitive(root, name));
}

int main(int argc, char **argv) {
    struct stat st;
    if (argc != 3 || lstat(argv[1], &st) || !S_ISDIR(st.st_mode) ||
        st.st_uid != getuid() || (st.st_mode & 077)) return 2;
    if (lstat(argv[2], &st) || !S_ISREG(st.st_mode) || st.st_uid != getuid() || (st.st_mode & 077)) return 2;
    FILE *file = fopen(argv[2], "rb");
    if (!file) return 2;
    char data[8193];
    size_t size = fread(data, 1, sizeof(data) - 1, file);
    bool complete = feof(file);
    fclose(file);
    if (!complete || memchr(data, 0, size)) return 2;
    data[size] = 0;
    cJSON *root = cJSON_ParseWithOpts(data, NULL, true);
    const char *required[] = {"sn", "prodId", "model", "devTypeId", "devTypeName", "manuId", "manuName", "devName", "ac_key_hex"};
    for (size_t i = 0; i < sizeof(required) / sizeof(required[0]); ++i)
        if (!field(root, required[i])) return 2;
    if (!field(root, "sn")[0] || strlen(field(root, "sn")) > 39 ||
        strspn(field(root, "sn"), "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_") != strlen(field(root, "sn"))) return 2;
    const char *hex = field(root, "ac_key_hex");
    if (strlen(hex) != 96 || strspn(hex, "0123456789abcdefABCDEF") != 96) return 2;
    for (size_t i = 0; i < sizeof(ac_key); ++i) {
        unsigned int value;
        if (sscanf(hex + i * 2, "%2x", &value) != 1) return 2;
        ac_key[i] = (uint8_t)value;
    }
    IotcDeviceInfo device = {
        .sn = field(root, "sn"), .prodId = field(root, "prodId"), .subProdId = "",
        .model = field(root, "model"), .devTypeId = field(root, "devTypeId"),
        .devTypeName = field(root, "devTypeName"), .manuId = field(root, "manuId"),
        .manuName = field(root, "manuName"), .devName = field(root, "devName"),
        .fwv = "0.1.0", .hwv = "0.1.0", .swv = "0.1.0", .protType = 1,
    };
    if (ModelDevInfoInit(&device)) return 2;
    SecurityRegUdidCallback(ModelGetUdid);
    SecurityRegAcKeyCallback(get_key);
    SecurityStoreCallback callbacks = {
        .onInit = IotcKvInit, .onDeinit = IotcKvDeInit,
        .onSetValue = IotcKvSetValue, .onGetValue = IotcKvGetValue,
        .onGetValueLen = IotcKvGetLen, .onDelValue = IotcKvDelValue,
    };
    int result = SecurityStoreInit(argv[1], &callbacks);
    if (result) return 2;
    result = ConfigInfoInit();
    int pulled = SecurityStorePull(IOTC_STORE_KEY_INT_DEVICE_CONFIG);
    uint8_t device_id[256] = {0};
    uint32_t length = sizeof(device_id);
    int loaded = ConfigInfoGet(CONFIG_INFO_KEY_DEVICE_ID, device_id, &length);
    bool bound = pulled == 0 && loaded == 0 && length > 0 && device_id[0] != 0;
    uint8_t digest[32];
    char hex_digest[65] = {0};
    if (bound) {
        if (mbedtls_sha256_ret(device_id, strnlen((char *)device_id, length), digest, 0)) return 2;
        for (size_t i = 0; i < sizeof(digest); ++i) snprintf(hex_digest + i * 2, 3, "%02x", digest[i]);
    }
    printf("IDENTITY_PROBE {\"config_init\":%d,\"storage_read\":%d,\"bound_identity_loaded\":%s,\"dev_id_sha256\":\"%s\",\"network_started\":false,\"fields\":{",
        result, pulled, bound ? "true" : "false", hex_digest);
    const ConfigInfoKey keys[] = {CONFIG_INFO_KEY_LOGIN_SECRET, CONFIG_INFO_KEY_LOGIN_PSK,
        CONFIG_INFO_KEY_UID_HASH, CONFIG_INFO_KEY_AUTHCODE_ID, CONFIG_INFO_KEY_AUTHCODE};
    const char *names[] = {"secret", "psk", "uid_hash", "auth_id", "auth_code"};
    for (size_t i = 0; i < sizeof(keys) / sizeof(keys[0]); ++i) {
        uint8_t value[256] = {0}; uint32_t size = sizeof(value);
        bool ok = bound && ConfigInfoGet(keys[i], value, &size) == 0;
        if (ok) {
            size_t used = (i == 1 || i == 4) ? 16 : strnlen((char *)value, size);
            if (mbedtls_sha256_ret(value, used, digest, 0)) return 2;
            for (size_t j = 0; j < sizeof(digest); ++j) snprintf(hex_digest + j * 2, 3, "%02x", digest[j]);
        }
        printf("%s\"%s\":\"%s\"", i ? "," : "", names[i], ok ? hex_digest : "");
        memset(value, 0, sizeof(value));
    }
    printf("}}\n");
    ConfigInfoDeinit();
    SecurityStoreDeinit();
    ModelDevInfoDeinit();
    cJSON_Delete(root);
    memset(ac_key, 0, sizeof(ac_key));
    return 0;
}
