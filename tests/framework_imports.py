"""只导入公共业务模块；不执行 HA 集成入口或原生 SDK。"""
import importlib
from pathlib import Path
import sys
import types


COMPONENT = Path(__file__).resolve().parents[1] / "custom_components/hwhomebridge"
PACKAGE = "_hwhomebridge_core_tests"
if PACKAGE not in sys.modules:
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT)]
    sys.modules[PACKAGE] = package


def load(name):
    return importlib.import_module(f"{PACKAGE}.{name}")
