/* 实际云端控制使用奇数字节偏移的 CoAP 扩展值；保持 UBSan 开启。 */
#include "coap_codec_comm.h"
#include "iotc_errcode.h"
#include "seq_num_utils.h"
#include "m2m_cloud_sess.h"
#include <assert.h>
#include <stdalign.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

extern SessCode CloudTlsSessSendUpdateSeqProcess(SessMsg *, UtilsBuffer *, SessAddtlInfo *);

int main(void) {
    alignas(8) uint8_t delta[] = {0xe1, 0x01, 0x02, 0x7f};
    CoapData raw = {.data = delta, .len = sizeof(delta)};
    CoapPacket packet = {0};
    uint32_t pos = 0;
    assert(CoapCommParseOptions(&packet, &raw, &pos) == IOTC_OK);
    assert(pos == sizeof(delta) && packet.options[0].option == 527);
    assert(packet.options[0].value.len == 1 && packet.options[0].value.data[0] == 0x7f);

    alignas(8) uint8_t length[272] = {0x1e, 0, 0};
    memset(length + 3, 0x5a, 269);
    raw = (CoapData){.data = length, .len = sizeof(length)};
    packet = (CoapPacket){0}; pos = 0;
    assert(CoapCommParseOptions(&packet, &raw, &pos) == IOTC_OK);
    assert(pos == sizeof(length) && packet.options[0].option == 1);
    assert(packet.options[0].value.len == 269 && packet.options[0].value.data[268] == 0x5a);

    raw = (CoapData){.data = delta, .len = 2};
    packet = (CoapPacket){0}; pos = 0;
    assert(CoapCommParseOptions(&packet, &raw, &pos) != IOTC_OK);
    alignas(8) uint8_t sequence[5] = {0xff, 0x12, 0x34, 0x56, 0x78};
    assert(CoapSeqToNum(sequence + 1, 2) == 0x1234);
    assert(CoapSeqToNum(sequence + 1, 4) == 0x12345678);
    uint32_t next_sequence = 0x01020304;
    M2mCloudContext cloud = {0};
    cloud.linkInfo.sessData = &next_sequence;
    packet = (CoapPacket){.opNum = 1};
    packet.options[0] = (CoapOption){.option = COAP_OPTION_TYPE_SEQ_NUM_ID,
        .value = {.data = sequence + 1, .len = 4}};
    UtilsBuffer buffer = {.buffer = sequence, .len = sizeof(sequence), .size = sizeof(sequence)};
    SocketAddr address = {0};
    SessAddtlInfo info = {.addr = &address, .userData = &cloud};
    assert(CloudTlsSessSendUpdateSeqProcess(&packet, &buffer, &info) == SESS_CODE_CONTINUE);
    assert(sequence[0] == 0xff && sequence[1] == 1 && sequence[2] == 2 && sequence[3] == 3 && sequence[4] == 4);
    assert(next_sequence == 0x01020305);
    puts("CoAP unaligned extensions, truncated input, sequence read and TLS response write: PASS");
    return 0;
}
