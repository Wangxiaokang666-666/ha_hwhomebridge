"""在独立 Linux 宿主只读运行；识别模拟和容器范围，不启动 HA 或 SDK。"""
import json
from pathlib import Path
import platform
import subprocess


def classify(system, machine, cpuinfo, virtualization, hypervisor):
    x86_cpu = any('vendor_id' in line and any(vendor in line for vendor in
        ('GenuineIntel', 'AuthenticAMD', 'HygonGenuine')) for line in cpuinfo.splitlines())
    emulated = virtualization == 'qemu' or 'tcg' in hypervisor.lower()
    host_scope = virtualization in {'none', 'kvm', 'vmware', 'microsoft', 'oracle', 'parallels', 'xen'}
    eligible = system == 'Linux' and machine == 'x86_64' and x86_cpu and host_scope and not emulated
    return {'system': system, 'reported_machine': machine, 'x86_cpu_markers': x86_cpu,
        'virtualization': virtualization, 'hypervisor': hypervisor,
        'software_emulation_detected': emulated, 'native_x86_host_candidate': eligible,
        'native_sdk_runtime_verified': False, 'production_access': False}


def command(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10,
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        return result.returncode, result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, ''


def probe():
    code, virtual = command(['systemd-detect-virt'])
    if code != 0 and not (code == 1 and virtual == 'none'):
        virtual = 'unknown'
    _, output = command(['lscpu', '--json'])
    try:
        cpu = {row['field']: row['data'] for row in json.loads(output)['lscpu']}
    except (ValueError, KeyError, TypeError):
        cpu = {}
    try:
        cpuinfo = Path('/proc/cpuinfo').read_text()
    except OSError:
        cpuinfo = ''
    return classify(platform.system(), platform.machine(), cpuinfo, virtual,
        cpu.get('Hypervisor vendor:', 'unknown'))


if __name__ == '__main__':
    print(json.dumps(probe()))
