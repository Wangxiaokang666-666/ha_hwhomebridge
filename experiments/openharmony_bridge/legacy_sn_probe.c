/* 仅作离线合成SN验收，不是可用于生产的扩展ABI。
 * 驱动必须先验证库SHA256为
 * 0579e6f3cee0320d1533e5a96cc6d72a065a43050eff4edcb7afd6a9a52c7b58，
 * 在无网络mount namespace把合成目录挂到/usrdata/hilink，并跟踪系统调用。
 * 不调用main、exit_brg或任何配对/云接口。版本改变必须重新审阅偏移。
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    FILE *input = fopen("/usrdata/hilink/device_sn", "rb");
    char raw[48] = {0};
    if (!input) return 3;
    size_t size = fread(raw, 1, sizeof(raw), input);
    fclose(input);
    if (size < 9 || size == sizeof(raw) || memcmp(raw, "SNPROBE-", 8)) return 4;
    void *library = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!library) return 5;
    const char *(*get_sn)(void) = dlsym(library, "GetGatewaySN");
    if (!get_sn) return 6;
    /* 此已固定版本GetDevInfo调用的无参文件初始化函数。 */
    void (*initialize_files)(void) = (void *)((uintptr_t)get_sn - 0xf5c0 + 0xeed0);
    initialize_files();
    const char *sn = get_sn();
    printf("NATIVE_SN ");
    for (size_t i = 0; sn && i < 40 && sn[i]; ++i) printf("%02x", (unsigned char)sn[i]);
    puts("");
    fflush(stdout);
    _exit(0);
}
