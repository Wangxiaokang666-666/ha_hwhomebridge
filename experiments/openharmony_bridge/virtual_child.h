#ifndef POC_VIRTUAL_CHILD_H
#define POC_VIRTUAL_CHILD_H
#include <stdbool.h>
#include "iotc_prof_def.h"

/* 单个、确定性身份的独立虚拟开关；没有 HA 接口。 */
int PocChildInit(const char *parent_sn, bool enabled);
bool PocChildEnabled(void);
bool PocChildMatches(const char *device_id);
bool PocChildRootMatches(const char *device_id);
int PocChildGet(const IotcCharState *state, char **out, uint32_t *len);
int PocChildPut(const IotcCharState *state);
void PocChildCloudOnline(void);
void PocChildCloudOffline(void);
#endif
