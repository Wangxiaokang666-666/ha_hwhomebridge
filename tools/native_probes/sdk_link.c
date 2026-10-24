#include "hilink_profile_bridge.h"
/* 仅用于目标架构链接检查，不运行产物、不注册设备。 */
int main(void) { return HilinkSyncBrgDevStatus("LAB-NOT-EXECUTED", DEV_ONLINE); }
