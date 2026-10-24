/* 只运行子设备状态/路由，不启动 SDK 或网络。由 Python 在临时目录调用。 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "virtual_child.h"
int main(int argc, char **argv) {
    if (argc < 3 || chdir(argv[1]) || PocChildInit("OHPOC-local-child-smoke", true)) return 2;
    if (!strcmp(argv[2], "init")) return 0;
    if (argc < 5) return 2;
    IotcCharState state = {.devId = argv[3], .svcId = argv[4]};
    if (!strcmp(argv[2], "put") && argc == 6) {
        state.data = argv[5]; state.len = (uint32_t)strlen(argv[5]);
        return PocChildPut(&state) ? 2 : 0;
    }
    if (!strcmp(argv[2], "get") && argc == 5) {
        char *out = NULL; uint32_t len = 0;
        if (PocChildGet(&state, &out, &len)) return 2;
        puts(out); free(out); return 0;
    }
    return 2;
}
