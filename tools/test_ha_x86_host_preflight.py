"""阻止把模拟用户态、TCG 或容器检测当作原生宿主验收。"""
import unittest

from ha_x86_host_preflight import classify


class X86HostPreflightTests(unittest.TestCase):
    def test_emulated_uname_with_arm_cpu_is_not_native(self):
        result = classify('Linux', 'x86_64', 'CPU architecture: 8', 'none', 'unknown')
        self.assertFalse(result['native_x86_host_candidate'])

    def test_full_system_tcg_is_not_native(self):
        result = classify('Linux', 'x86_64', 'vendor_id: GenuineIntel', 'qemu', 'TCG')
        self.assertFalse(result['native_x86_host_candidate'])
        self.assertTrue(result['software_emulation_detected'])

    def test_x86_host_is_only_a_candidate_until_sdk_runs(self):
        result = classify('Linux', 'x86_64', 'vendor_id: AuthenticAMD', 'kvm', 'KVM')
        self.assertTrue(result['native_x86_host_candidate'])
        self.assertFalse(result['native_sdk_runtime_verified'])

    def test_container_or_unknown_scope_requires_host_probe(self):
        for scope in ('docker', 'podman', 'unknown'):
            with self.subTest(scope=scope):
                result = classify('Linux', 'x86_64', 'vendor_id: GenuineIntel', scope, 'unknown')
                self.assertFalse(result['native_x86_host_candidate'])
