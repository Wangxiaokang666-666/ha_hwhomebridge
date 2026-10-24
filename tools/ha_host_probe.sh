#!/bin/sh
# 只读宿主探针；经已认证SSH的标准输入执行，不上传文件，不调用容器exec。
set -eu
container=$1
interface=$2
case "$container" in ''|*[!a-zA-Z0-9_.-]*) exit 2;; esac
case "$interface" in ''|*[!a-zA-Z0-9_.:-]*) exit 2;; esac
record() { printf '%s\t%s\n' "$1" "$2"; }
record version 1
record uid "$(id -u)"
record init "$(cat /proc/1/comm)"
if [ -e /.dockerenv ] || [ -e /run/.containerenv ]; then record container_context true; else record container_context false; fi
# 后面的宿主路径和进程检查只能在真正的root宿主执行。
[ "$(id -u)" = 0 ] && [ "$(cat /proc/1/comm)" = systemd ]
[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ]
unset DOCKER_CONTEXT DOCKER_TLS_VERIFY DOCKER_CERT_PATH
export DOCKER_HOST=unix:///var/run/docker.sock
record docker_info "$(docker info --format '{"id":{{json .ID}},"root":{{json .DockerRootDir}},"security":{{json .SecurityOptions}}}')"
record owner "$(docker inspect --format '{"id":{{json .Id}},"state":{{json .State}},"network":{{json .HostConfig.NetworkMode}},"restart":{{json .HostConfig.RestartPolicy}},"user":{{json .Config.User}}}' "$container")"
record mounts "$(docker inspect --format '{{json .Mounts}}' "$container")"
config=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{"\n"}}{{end}}{{end}}' "$container")
case "$config" in /*) ;; *) exit 2;; esac
case "$config" in *'
'*|*"$(printf '\t')"*) exit 2;; esac
[ "$(readlink -f "$config")" = "$config" ]
record config "$config"
for relative in . custom_components/hwhomebridge .storage/hwhomebridge .storage/core.config_entries .storage/core.device_registry .storage/core.entity_registry; do
    if [ "$relative" = . ]; then path=$config; else path=$config/$relative; fi
    [ ! -L "$path" ]
    [ "$(readlink -f "$path")" = "$path" ]
    record "stat:$relative" "$(stat -c '{"uid":%u,"gid":%g,"mode":"%a","type":"%F","size":%s}' "$path")"
done
pid=$(docker inspect --format '{{.State.Pid}}' "$container")
if [ "$pid" -gt 0 ]; then
    record runtime_uid "$(awk '/^Uid:/ {print $2}' "/proc/$pid/status")"
    record host_net "$(readlink /proc/1/ns/net)"
    record core_net "$(readlink "/proc/$pid/ns/net")"
fi
record interface_mac "$(cat "/sys/class/net/$interface/address")"
record python "$(command -v python3 || true)"
for protocol in tcp tcp6 udp udp6; do
    record "ports:$protocol" "$(awk 'NR>1 {print $2}' "/proc/net/$protocol" | tr '\n' ' ')"
done
record complete true
