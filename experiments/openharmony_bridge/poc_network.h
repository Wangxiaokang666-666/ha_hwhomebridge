#ifndef POC_NETWORK_H
#define POC_NETWORK_H
#include <stdint.h>
/* 未提供覆盖值时保留构建端口；非法覆盖值返回 0。 */
uint16_t PocGetLanPort(uint16_t fallback);
#endif
