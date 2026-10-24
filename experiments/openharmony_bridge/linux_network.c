/* Linux LAN 适配；只读已有网络，不更改宿主/HA 网络，不模拟 Wi-Fi 成功。 */
#include "iotc_network.h"
#include "iotc_wifi.h"
#include "iotc_errcode.h"
#include "poc_network.h"
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <netpacket/packet.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

uint16_t PocGetLanPort(uint16_t fallback) {
    const char *value = getenv("POC_LAN_PORT");
    if (!value) return fallback;
    size_t size = strlen(value);
    if (!size || size > 5 || strspn(value, "0123456789") != size) return 0;
    unsigned long port = strtoul(value, NULL, 10);
    return port >= 1024 && port <= 65535 ? (uint16_t)port : 0;
}

static const char *iface(void) { return getenv("POC_INTERFACE"); }
static int selected(const struct ifaddrs *a) {
    return a->ifa_addr && iface() && strcmp(a->ifa_name, iface()) == 0 &&
        (a->ifa_flags & IFF_UP) && (a->ifa_flags & IFF_RUNNING) && !(a->ifa_flags & IFF_LOOPBACK);
}
static int32_t address(char *buf, uint32_t len, int broadcast) {
    struct ifaddrs *all = NULL;
    if (!buf || !len || !iface() || getifaddrs(&all)) return IOTC_ERR_PARAM_INVALID;
    int32_t result = IOTC_ADAPTER_NETWORK_ERR_GET_IP;
    for (struct ifaddrs *a = all; a; a = a->ifa_next) {
        if (!selected(a) || a->ifa_addr->sa_family != AF_INET) continue;
        struct sockaddr *s = broadcast ? a->ifa_broadaddr : a->ifa_addr;
        if (s && inet_ntop(AF_INET, &((struct sockaddr_in *)s)->sin_addr, buf, len)) result = IOTC_OK;
        break;
    }
    freeifaddrs(all);
    return result;
}
int32_t IotcGetLocalIp(char *b, uint32_t n) { return address(b, n, 0); }
int32_t IotcGetBroadcastAddr(char *b, uint32_t n) { return address(b, n, 1); }
int32_t IotcGetSoftApIp(char *b, uint32_t n) { (void)b; (void)n; return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcGetMacAddr(uint8_t *b, uint32_t n) {
    struct ifaddrs *all = NULL;
    if (!b || n < 6 || !iface() || getifaddrs(&all)) return IOTC_ERR_PARAM_INVALID;
    int32_t result = IOTC_ADAPTER_NETWORK_ERR_GET_MAC;
    for (struct ifaddrs *a = all; a; a = a->ifa_next) {
        if (!selected(a) || a->ifa_addr->sa_family != AF_PACKET) continue;
        const struct sockaddr_ll *s = (const struct sockaddr_ll *)a->ifa_addr;
        if (s->sll_halen == 6) { memcpy(b, s->sll_addr, 6); result = IOTC_OK; }
        break;
    }
    freeifaddrs(all);
    return result;
}
IotcNetworkState IotcGetNetworkState(void) {
    char ip[INET_ADDRSTRLEN];
    return IotcGetLocalIp(ip, sizeof(ip)) == IOTC_OK ? IOTC_NETWORK_CONNECTED : IOTC_NETWORK_NOT_CONNECTED;
}
int32_t IotcGetWifiMode(void) {
    /* SDK 以 STATION 表示 LAN 连接；产品协议类型由调用方显式配置。 */
    return IotcGetNetworkState() == IOTC_NETWORK_CONNECTED ? IOTC_WIFI_MODE_STATION : IOTC_WIFI_MODE_INVALID;
}
int32_t IotcGetLastConnectResult(void) {
    return IotcGetNetworkState() == IOTC_NETWORK_CONNECTED ? IOTC_WIFI_CONNECT_OK : IOTC_WIFI_CONNECT_ERROR;
}
int32_t IotcConnectWifi(void) { return IotcGetNetworkState() == IOTC_NETWORK_CONNECTED ? IOTC_OK : IOTC_ERR_NOT_SUPPORT; }
int32_t IotcReconnectWifi(void) { return IotcConnectWifi(); }
int32_t IotcRegisterEventCallback(const IotcWifiEvent *cb) { (void)cb; return IOTC_ERR_NOT_SUPPORT; }
void IotcUnregEventCallback(void) {}
int32_t IotcGetWifiInfo(uint8_t *s, uint32_t *sn, uint8_t *p, uint32_t *pn) {
    (void)s; (void)sn; (void)p; (void)pn; return IOTC_ERR_NOT_SUPPORT;
}
int32_t IotcSetWifiInfo(const uint8_t *s, uint32_t sn, const uint8_t *p, uint32_t pn) {
    (void)s; (void)sn; (void)p; (void)pn; return IOTC_ERR_NOT_SUPPORT;
}
int32_t IotcDeleteWifiInfo(void) { return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcDisconnectWifi(void) { return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcRestartWifi(void) { return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcConnectWifiByBssid(int32_t t, const uint8_t *b, uint32_t n) { (void)t; (void)b; (void)n; return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcScanWifi(const IotcWifiScanParam *p) { (void)p; return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcGetWifiScanResult(IotcWifiList **p) { (void)p; return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcFreeWifiScanResult(IotcWifiList *p) { free(p); return IOTC_OK; }
int32_t IotcGetWifiBssid(uint8_t *p, uint32_t n) { (void)p; (void)n; return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcGetWifiRssi(int8_t *p) { (void)p; return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcStartSoftAp(const uint8_t *s, uint32_t sn, const uint8_t *p, uint32_t pn) {
    (void)s; (void)sn; (void)p; (void)pn; return IOTC_ERR_NOT_SUPPORT;
}
int32_t IotcStopSoftAp(void) { return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcGetSoftapStationInfo(IotcStationList **p) { (void)p; return IOTC_ERR_NOT_SUPPORT; }
void IotcFreeSoftapStationInfo(IotcStationList *p) { free(p); }
int32_t IotcSoftapDisassociateSta(uint8_t *p, uint32_t n) { (void)p; (void)n; return IOTC_ERR_NOT_SUPPORT; }
int32_t IotcSoftapAddTxPower(int p) { (void)p; return IOTC_ERR_NOT_SUPPORT; }
