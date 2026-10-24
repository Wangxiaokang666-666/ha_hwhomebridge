/* 独立进程、临时身份目录、回环 mock；不启动 SDK，不访问真实 HA。 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "ha_children.h"
int main(int argc, char **argv) {
    if (argc < 3 || chdir(argv[1]) || PocHaInit("OHPOC-local-ha-smoke", true)) return 2;
    if (!strcmp(argv[2], "init")) return 0;
    if (argc < 5) return 2;
    IotcCharState state = {.devId=argv[3], .svcId=argv[4]};
    if (!strcmp(argv[2], "get") && argc == 5) {
        char *out = NULL; uint32_t length = 0;
        if (PocHaGet(&state, &out, &length)) return 2;
        puts(out); free(out); return 0;
    }
    if (!strcmp(argv[2], "put") && argc == 6) {
        state.data=argv[5]; state.len=(uint32_t)strlen(argv[5]);
        return PocHaPut(&state) ? 2 : 0;
    }
    return 2;
}
