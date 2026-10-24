#ifndef POC_HA_CHILDREN_H
#define POC_HA_CHILDREN_H
#include <stdbool.h>
#include "iotc_prof_def.h"

/* 可选的 HA 子设备；token 不进入 Linux。 */
int PocHaInit(const char *parent_sn, bool enabled);
bool PocHaEnabled(void);
bool PocHaMatches(const char *device_id);
int PocHaGet(const IotcCharState *state, char **out, uint32_t *len);
int PocHaPut(const IotcCharState *state);
void PocHaCloudOnline(void);
void PocHaCloudOffline(void);
#endif
