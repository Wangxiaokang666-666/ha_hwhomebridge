#include <stddef.h>
#include <stdio.h>
#include "hilink_profile_bridge.h"
int main(void) {
    printf("{\"BrgDevInfo_size\":%zu,\"prodId_offset\":%zu,\"protType_offset\":%zu,\"BrgDevSvcInfo_size\":%zu,\"service_limit\":%d}\n",
        sizeof(BrgDevInfo), offsetof(BrgDevInfo, prodId), offsetof(BrgDevInfo, protType), sizeof(BrgDevSvcInfo), HILINK_M2M_MAX_SVC_NUM);
    return 0;
}
