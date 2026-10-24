/* 固定公开测试向量；不读取运行中的设备身份、PIN、AC 或云凭据。 */
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include "security_speke_nego_ctx.h"
#include "iotc_mem.h"
#include "iotc_md.h"
#include "utils_common.h"
#include "security_random.h"
#include "security_speke.h"

static int32_t test_pin(SpekeSession *session, void *user, uint8_t *out, uint32_t *len) {
    (void)session; (void)user;
    if (*len < 8) return -1;
    memcpy(out, "12345678", 8); *len = 8;
    return 0;
}

static int self_handshake(void) {
    SpekeCallback cb = {.getPinCode = test_pin};
    SpekeSession *client = SpekeInitSession(SPEKE_TYPE_CLIENT, &cb, NULL);
    SpekeSession *server = SpekeInitSession(SPEKE_TYPE_SERVER, &cb, NULL);
    if (!client || !server) return 10;
    uint8_t *req = NULL, *rsp = NULL, *cfm = NULL, *done = NULL, *unused = NULL;
    uint32_t a = 0, b = 0, c = 0, d = 0, e = 0;
    int ret = SpekeStartSession(client, &req, &a);
    printf("SELF start %d\n", ret);
    if (!ret) ret = SpekeProcessPacket(server, (const char *)req, &rsp, &b);
    printf("SELF server_response %d\n", ret);
    if (!ret) ret = SpekeProcessPacket(client, (const char *)rsp, &cfm, &c);
    printf("SELF client_confirm %d\n", ret);
    if (!ret) ret = SpekeProcessPacket(server, (const char *)cfm, &done, &d);
    printf("SELF server_confirm %d\n", ret);
    if (!ret) ret = SpekeProcessPacket(client, (const char *)done, &unused, &e);
    printf("SELF client_finish %d\n", ret);
    IotcFree(req); IotcFree(rsp); IotcFree(cfm); IotcFree(done); IotcFree(unused);
    SpekeFreeSession(client); SpekeFreeSession(server);
    return ret ? 11 : 0;
}

static void digest(const char *label, const uint8_t *data, uint32_t len) {
    uint8_t sha[32];
    if (IotcMdCalc(IOTC_MD_SHA256, data, len, sha, sizeof(sha))) return;
    printf("VECTOR %s ", label);
    for (size_t i = 0; i < sizeof(sha); ++i) printf("%02x", sha[i]);
    printf("\n");
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    if (SecurityRandomInit()) return 8;
    if (!strcmp(argv[1], "--self")) return self_handshake();
    if (strlen(argv[1]) != 512) return 2;
    const uint8_t pin[] = "12345678"; /* 固定测试 PIN，非任何运行设备的 PIN。 */
    uint8_t salt[16]; memset(salt, 0x33, sizeof(salt));
    NegoContext *ctx = NegoContextInit(pin, 8, salt, sizeof(salt), PRIME_NORMAL);
    if (!ctx || IotcMpiReadString(ctx->random, 16, "1")) return 3;
    IotcFree(ctx->pubKey); ctx->pubKey = NULL; ctx->pubKeyLen = 0;
    if (NegoContextRegenPubKey(ctx, pin, 8)) return 4;
    digest("generator", ctx->pubKey, ctx->pubKeyLen);
    uint8_t peer[256];
    if (!UtilsUnhexify(argv[1], 512, peer, sizeof(peer))) return 5;
    if (NegoContextGenSessionKey(ctx, peer, sizeof(peer))) return 6;
    digest("identity_key", ctx->identityEncKey, sizeof(ctx->identityEncKey));
    digest("hmac_key", ctx->hmacKey, sizeof(ctx->hmacKey));
    memset(ctx->localChallenge, 0x11, sizeof(ctx->localChallenge));
    memset(ctx->remoteChallenge, 0x22, sizeof(ctx->remoteChallenge));
    uint8_t mac[32];
    if (NegoContextGenHmac(ctx, mac, sizeof(mac))) return 7;
    digest("hmac", mac, sizeof(mac));
    NegoContextFree(ctx);
    return 0;
}
