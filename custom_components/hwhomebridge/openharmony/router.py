"""历史实验环境的薄适配；不提供第二套公共能力实现。"""
from pathlib import Path

from ..capability_router import CapabilityRouter
from .configuration import OpenHarmonyRegistry, child_sn, read_json


class OpenHarmonyRouter(CapabilityRouter):
    registry_class = OpenHarmonyRegistry

    @classmethod
    async def create(cls, transport, configuration, instances):
        # 旧实验实例使用人工选定的角色白名单；正式调用方必须传入HA设备清单。
        # 保留已配对实验子卡的SN算法，不把OHPOC身份规则带到公共框架。
        owned = {source["id"]: set(source["roles"].values()) for source in instances.get("devices", [])}
        if not isinstance(instances.get("parent_sn"), str) or not instances["parent_sn"].startswith("OHPOC-"):
            raise ValueError("independent OHPOC parent required")
        definitions = read_json(Path(configuration) / "product_registry.json")
        return await super().create(
            transport, configuration, instances, owned_entities=owned,
            supported_pids={item["pid"] for item in definitions["products"]},
            sn_factory=lambda parent, source, key: child_sn(parent, key))

    def set_write_policy(self, policy):
        if policy not in ("fan_only", "read_only"):
            raise ValueError("unsupported OpenHarmony write policy")
        entities = {roles[role] for roles in self.roles.values()
                    for role in ("fan", "fan_timer_enable", "fan_timer_duration")} if policy == "fan_only" else set()
        self.set_write_entities(entities)
