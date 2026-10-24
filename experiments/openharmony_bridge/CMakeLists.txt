cmake_minimum_required(VERSION 3.16)
project(hwhomebridge_poc C)
if(NOT CMAKE_SYSTEM_NAME STREQUAL "Linux")
  message(FATAL_ERROR "实验必须在 Linux 编译；先运行 bootstrap.py")
endif()
set(CMAKE_C_STANDARD 11)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)
option(POC_AILIFE_LEGACY_DISCOVERY "独立实验兼容智慧生活 cloudSetup 发现入口" OFF)
set(POC_LAN_PORT 5683 CACHE STRING "独立实验 LAN UDP 监听端口")
set(POC_LOG_LEVEL 3 CACHE STRING "SDK 日志级别，3=告警，4=状态变化")
if(NOT POC_LOG_LEVEL MATCHES "^[1-4]$")
  message(FATAL_ERROR "POC_LOG_LEVEL 必须在 1–4 之间；不启用载荷调试日志")
endif()
if(NOT POC_LAN_PORT MATCHES "^[0-9]+$" OR POC_LAN_PORT LESS 1024 OR POC_LAN_PORT GREATER 65535)
  message(FATAL_ERROR "POC_LAN_PORT 必须在 1024–65535 之间")
endif()
# SDK 需要设置证书校验的时间回调；TLS 库和调用方保持同一配置。
add_compile_definitions(MBEDTLS_PLATFORM_TIME_ALT)
set(DEP "${CMAKE_CURRENT_SOURCE_DIR}/_deps")
set(OH "${DEP}/iot_connect")
set(ENABLE_PROGRAMS OFF CACHE BOOL "" FORCE)
set(ENABLE_TESTING OFF CACHE BOOL "" FORCE)
add_subdirectory("${DEP}/mbedtls" "deps/mbedtls" EXCLUDE_FROM_ALL)
set(ENABLE_CJSON_TEST OFF CACHE BOOL "" FORCE)
set(ENABLE_CJSON_UTILS OFF CACHE BOOL "" FORCE)
set(BUILD_SHARED_LIBS OFF CACHE BOOL "" FORCE)
add_subdirectory("${DEP}/cjson" "deps/cjson" EXCLUDE_FROM_ALL)
file(GLOB SECUREC_SRC "${DEP}/securec/src/*.c")
add_library(securec STATIC ${SECUREC_SRC})
target_include_directories(securec PUBLIC "${DEP}/securec/include")
add_subdirectory("${OH}/core/infrastructure" "oh/core/infrastructure")
add_subdirectory("${OH}/core/device" "oh/core/device")
add_subdirectory("${OH}/core/wifi" "oh/core/wifi")
add_subdirectory("${OH}/sdk" "oh/sdk")
# 当前 Wi-Fi GN 清单比上游 CMake 完整，包含 TCP、PSK、认证码和云消息回复。
list(FILTER IOTC_SRC_LIST EXCLUDE REGEX "/core/wifi/")
file(STRINGS "${OH}/core/wifi/core_wifi.gni" WIFI_GN_LINES REGEX "\\.c\"")
foreach(line IN LISTS WIFI_GN_LINES)
  string(REGEX REPLACE ".*\"\\$wifi_core_path/([^\"]+)\".*" "\\1" relative "${line}")
  if(NOT EXISTS "${OH}/core/wifi/${relative}")
    message(FATAL_ERROR "Wi-Fi GN 源码清单解析失败: ${line}")
  endif()
  list(APPEND IOTC_SRC_LIST "${OH}/core/wifi/${relative}")
endforeach()
# 智慧生活旧版 TLS 使用两字节长度前缀；固定到上游改为 TCP/PSK 前的编解码器。
list(FILTER IOTC_SRC_LIST EXCLUDE REGEX "/coap_codec_tcp_v1\\.c$")
list(APPEND IOTC_SRC_LIST "${DEP}/tls_codec.c")
list(APPEND IOTC_SRC_LIST "${OH}/core/infrastructure/service/iotc_svc_ble.c")
# 对应上游 GN 的 Wi-Fi-only 选择，不链接 OpenHarmony 蓝牙/星闪驱动。
list(FILTER IOTC_SRC_LIST EXCLUDE REGEX "/iotc_oh_(ble|sle)\\.c$")
list(FILTER IOTC_SRC_LIST EXCLUDE REGEX "/iotc_oh_sle_adv_data\\.c$")
file(GLOB CRYPTO_SRC "${OH}/adapter/crypto/mbedtls/*.c")
add_library(oh_poc STATIC ${IOTC_SRC_LIST} ${CRYPTO_SRC}
  "${OH}/adapter/json/cjson/iotc_json.c"
  "${OH}/adapter/kv/posix/iotc_kv.c"
  "${OH}/adapter/log/printf/iotc_log.c"
  "${OH}/adapter/log/iotc_log_inner.c"
  "${OH}/adapter/mem/stdlib/iotc_mem.c"
  "${OH}/adapter/os/posix/iotc_os.c"
  "${OH}/adapter/socket/iotc_socket.c"
  "${OH}/adapter/tls/mbedtls/iotc_tls.c"
  linux_network.c)
set_source_files_properties("${OH}/core/wifi/application/lan_search/lan_search_coap_stack.c"
  PROPERTIES COMPILE_DEFINITIONS "IOTC_POC_LAN_PORT=${POC_LAN_PORT}"
  INCLUDE_DIRECTORIES "${CMAKE_CURRENT_SOURCE_DIR}")
target_include_directories(oh_poc PUBLIC ${IOTC_INC_LIST}
  "${OH}/interfaces/kits/common" "${OH}/interfaces/kits/oh_connect"
  "${OH}/adapter/include" "${DEP}/mbedtls/include" "${DEP}/cjson")
set_source_files_properties("${DEP}/tls_codec.c" PROPERTIES
  INCLUDE_DIRECTORIES "${OH}/core/wifi/infrastructure/transport/coap/codec")
target_compile_definitions(oh_poc PUBLIC IOTC_CONF_WIFI_SUPPORT=1
  IOTC_CONF_TCP_SUPPORT=0
  IOTC_CONNECT_WIFI_CLOUD_SUPPORT=1
  IOTC_CONF_AILIFE_SUPPORT=1 IOTC_CONF_ADAPTER_MBEDTLS_MAJOR_VERSION=2
  IOTC_CONF_KV_SUPPORT=1 IOTC_CONF_DEVICE_LEVEL=2 IOTC_CONF_DEV_TYPE=2 IOTC_CONF_OH_DEFAULT_TASK_SIZE=262144
  IOTC_CONF_DFX_WATCH_DOG_DEFAULT_TASK_STACK_SIZE=131072
  IOTC_CONF_LOG_BUILD_LEVEL=${POC_LOG_LEVEL} IOT_CONF_LOG_DEFAULT_LEVEL=${POC_LOG_LEVEL})
if(POC_AILIFE_LEGACY_DISCOVERY)
  target_compile_definitions(oh_poc PUBLIC IOTC_POC_HILINK_DISCOVERY_COMPAT=1)
endif()
target_link_libraries(oh_poc PUBLIC securec cjson mbedtls mbedx509 mbedcrypto pthread m)
add_executable(bridge_poc main.c virtual_child.c ha_children.c)
target_link_libraries(bridge_poc PRIVATE oh_poc)
target_compile_options(bridge_poc PRIVATE -Wall -Wextra -Werror)
add_executable(ha_child_state_test ha_child_state_test.c ha_children.c)
target_link_libraries(ha_child_state_test PRIVATE oh_poc)
target_compile_options(ha_child_state_test PRIVATE -Wall -Wextra -Werror)
add_executable(ha_child_availability_test ha_child_availability_test.c)
target_link_libraries(ha_child_availability_test PRIVATE oh_poc)
target_compile_options(ha_child_availability_test PRIVATE -Wall -Wextra -Werror)
add_executable(identity_state_probe identity_state_probe.c)
target_link_libraries(identity_state_probe PRIVATE oh_poc)
target_compile_options(identity_state_probe PRIVATE -Wall -Wextra -Werror)
add_executable(virtual_child_state_test virtual_child_state_test.c virtual_child.c)
target_link_libraries(virtual_child_state_test PRIVATE oh_poc)
target_compile_options(virtual_child_state_test PRIVATE -Wall -Wextra -Werror)
add_executable(speke_vectors speke_vectors.c)
target_include_directories(speke_vectors PRIVATE "${OH}/core/infrastructure/security/speke")
target_link_libraries(speke_vectors PRIVATE oh_poc)
target_compile_options(speke_vectors PRIVATE -Wall -Wextra -Werror)
add_executable(coap_alignment_test coap_alignment_test.c)
target_include_directories(coap_alignment_test PRIVATE "${OH}/core/wifi/infrastructure/transport/coap/codec")
target_link_libraries(coap_alignment_test PRIVATE oh_poc)
target_compile_options(coap_alignment_test PRIVATE -Wall -Wextra -Werror)
