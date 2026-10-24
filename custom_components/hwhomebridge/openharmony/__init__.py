"""可选 OpenHarmony 后端；不会自动接管或初始化现有 HiLink 后端。"""

from .router import OpenHarmonyRouter

__all__ = ["OpenHarmonyRouter"]
