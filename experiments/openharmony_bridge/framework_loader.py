"""独立实验只加载框架模块，不执行 HA 集成入口、不加载旧原生库。"""
import importlib
from pathlib import Path
import sys
import types


def load_framework():
    sys.dont_write_bytecode = True
    name = "_hwhomebridge_standalone"
    if name not in sys.modules:
        package = types.ModuleType(name)
        package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components" / "hwhomebridge")]
        sys.modules[name] = package
    return importlib.import_module(name + ".openharmony")
