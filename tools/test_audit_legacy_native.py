"""核对仓库内真实旧库；不能把符号检查当作风扇注册验收。"""
from pathlib import Path
import tempfile
import unittest

from elftools.common.exceptions import ELFError

from audit_legacy_native import audit, inspect_library, LEGACY_EXPORTS


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/hwhomebridge"


class LegacyNativeAuditTests(unittest.TestCase):
    def test_actual_two_architectures_have_only_legacy_exports(self):
        result = audit(COMPONENT)
        self.assertEqual(result["release_gate"], "not_met")
        self.assertEqual(result["bridge_sources"], [])
        self.assertEqual(result["single_gateway_new_fan_acceptance"], "not_run")
        self.assertEqual(len(result["libraries"]), 2)
        for library in result["libraries"]:
            with self.subTest(architecture=library["architecture"]):
                self.assertEqual(set(library["exports"]), LEGACY_EXPORTS)
                self.assertFalse(library["has_static_symbol_table"])
                self.assertTrue(all(value == "absent_from_dynsym"
                                    for value in library["sdk_adapter_hooks"].values()))

    def test_wrong_architecture_rejected(self):
        with self.assertRaisesRegex(ValueError, "architecture mismatch"):
            inspect_library(COMPONENT / "hilink_bridge/lib/amd64/libhilink_bridge.so", "aarch64")

    def test_non_elf_file_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "lib.so"
            path.write_bytes(b"not an ELF")
            with self.assertRaises(ELFError):
                inspect_library(path, "amd64")


if __name__ == "__main__":
    unittest.main()
