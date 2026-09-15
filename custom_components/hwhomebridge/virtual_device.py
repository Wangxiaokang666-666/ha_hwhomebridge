"""
Virtual Device - 虚拟设备模型

VirtualDevice 是聚合的核心。一个 VirtualDevice 对应一个 SN，
代表华为侧的一个逻辑设备，内部聚合了同一 HA device 下的多个 entity。

SN 管理也从这里实现：
- SN 由 device_id 的哈希生成，确保确定性和唯一性
- SN 与 VirtualDevice 的映射关系 (1:1)
- 提供 SN ↔ VirtualDevice 的双向查找
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from .product_registry import ProductDef, ServiceDef

_LOGGER = logging.getLogger(__name__)


@dataclass
class ServiceEntry:
    """VirtualDevice 中一个 service 对应的 HA entity 入口"""
    entity_id: str               # HA entity ID
    domain: str                  # HA domain (light/switch/sensor/fan...)
    service_name: str            # 对应的华为 service，如 "switch"


class VirtualDevice:
    """虚拟设备，对应华为侧的一个逻辑设备（一个 SN）"""

    def __init__(self, sn: str, product: ProductDef, ha_device_id: str):
        """
        Args:
            sn: 设备 SN，由 generate_sn() 生成
            product: 对应的华为产品定义
            ha_device_id: 对应的 HA device_id
        """
        self.sn: str = sn
        self.product: ProductDef = product
        self.ha_device_id: str = ha_device_id
        self.service_entries: dict[str, ServiceEntry] = {}  # service_id -> ServiceEntry
        self.online: bool = False
        self.switch_state: int = 0  # 缓存 switch 状态（用于 button 映射场景）
        self.last_commanded_enum: dict[str, str] = {}  # service_id -> 最后接收到的华为枚举值（用于非双射 enum_to_number 反向映射）

    def add_service_entry(self, service_id: str, entry: ServiceEntry):
        """添加 service -> entity 映射"""
        self.service_entries[service_id] = entry

    def get_entity_for_service(self, service_id: str) -> Optional[str]:
        """获取指定 service 对应的 entity_id"""
        entry = self.service_entries.get(service_id)
        if entry is not None:
            return entry.entity_id
        return None

    def route_action(self, service_id: str, payload: dict) -> Optional[str]:
        """根据 service 和 payload，路由到正确的 entity_id

        在基础实现中，直接通过 service_id 查找对应的 entity_id。
        对于需要更精细路由的场景（如同一个 service 对应多个 entity），
        可以在子类中覆盖此方法。

        Args:
            service_id: 华为 service ID，如 "switch"
            payload: 控制命令的 payload，如 {"on": 1}

        Returns:
            对应的 entity_id，找不到时返回 None
        """
        return self.get_entity_for_service(service_id)

    def get_service_for_entity(self, entity_id: str) -> Optional[str]:
        """根据 entity_id 反查对应的 service_id
        
        注意：如果一个 entity 映射到多个 service，只返回第一个。
        对于需要获取所有 service 的场景，请使用 get_all_services_for_entity()
        """
        for service_id, entry in self.service_entries.items():
            if entry.entity_id == entity_id:
                return service_id
        return None
    
    def get_all_services_for_entity(self, entity_id: str) -> List[str]:
        """获取 entity 映射到的所有 service_id
        
        用于处理一个 entity 对应多个服务的场景（如 light 同时映射到 switch 和 brightness）
        
        Args:
            entity_id: HA entity ID
            
        Returns:
            service_id 列表，可能为空
        """
        services = []
        for service_id, entry in self.service_entries.items():
            if entry.entity_id == entity_id:
                services.append(service_id)
        return services

    def get_all_entity_ids(self) -> list:
        """获取此虚拟设备关联的所有 entity_id"""
        return [entry.entity_id for entry in self.service_entries.values()]

    def is_available(self) -> bool:
        """检查虚拟设备是否可用（至少有一个可用的 entity）"""
        return len(self.service_entries) > 0

    def __repr__(self):
        return (f"VirtualDevice(sn={self.sn}, product={self.product.pid}, "
                f"device_id={self.ha_device_id}, "
                f"services={len(self.service_entries)})")


# ---------------------------------------------------------------------------
# SN 管理
# ---------------------------------------------------------------------------

class SNManager:
    """SN 管理器：管理 SN 与 VirtualDevice 的映射关系"""

    # SN 生成的固定前缀，用于区分不同来源的 SN
    SN_PREFIX = "HB"

    def __init__(self):
        self.sn_virtual_device_map: dict[str, VirtualDevice] = {}
        self.device_virtual_device_map: dict[str, VirtualDevice] = {}

    @staticmethod
    def generate_sn(device_id: str) -> str:
        """根据 HA device_id 生成 SN

        生成规则：SN = "HB" + SHA256(device_id)[:14]
        - 总长度 16 字符
        - "HB" 前缀标识来源
        - 基于哈希，确保确定性和唯一性

        Args:
            device_id: HA 的 device_id

        Returns:
            生成的 SN 字符串
        """
        digest = hashlib.sha256(device_id.encode('utf-8')).hexdigest()
        return f"{SNManager.SN_PREFIX}{digest[:14]}"

    def register(self, virtual_device: VirtualDevice) -> bool:
        """注册 VirtualDevice 到 SN 管理器

        Args:
            virtual_device: 要注册的虚拟设备

        Returns:
            True 表示注册成功，False 表示 SN 冲突
        """
        sn = virtual_device.sn
        device_id = virtual_device.ha_device_id

        if sn in self.sn_virtual_device_map:
            existing = self.sn_virtual_device_map[sn]
            if existing.ha_device_id != device_id:
                _LOGGER.error(f"SN conflict: {sn} already registered for device {existing.ha_device_id}, "
                              f"cannot register for {device_id}")
                return False

        self.sn_virtual_device_map[sn] = virtual_device
        self.device_virtual_device_map[device_id] = virtual_device
        _LOGGER.info(f"Registered VirtualDevice: sn={sn}, device_id={device_id}")
        return True

    def unregister(self, sn: str) -> Optional[VirtualDevice]:
        """注销 VirtualDevice

        Args:
            sn: 要注销的 SN

        Returns:
            被注销的 VirtualDevice，如果不存在返回 None
        """
        vd = self.sn_virtual_device_map.pop(sn, None)
        if vd is not None:
            self.device_virtual_device_map.pop(vd.ha_device_id, None)
            _LOGGER.info(f"Unregistered VirtualDevice: sn={sn}")
        return vd

    def get_by_sn(self, sn: str) -> Optional[VirtualDevice]:
        """根据 SN 获取 VirtualDevice"""
        return self.sn_virtual_device_map.get(sn)

    def get_by_device_id(self, device_id: str) -> Optional[VirtualDevice]:
        """根据 device_id 获取 VirtualDevice"""
        return self.device_virtual_device_map.get(device_id)

    def get_sn_by_device_id(self, device_id: str) -> Optional[str]:
        """根据 device_id 获取 SN"""
        vd = self.device_virtual_device_map.get(device_id)
        if vd is not None:
            return vd.sn
        return None

    def get_entity_sn(self, entity_id: str) -> Optional[str]:
        """根据 entity_id 查找其所属 VirtualDevice 的 SN

        Args:
            entity_id: HA entity ID

        Returns:
            SN 字符串，如果找不到返回 None
        """
        for vd in self.sn_virtual_device_map.values():
            for entry in vd.service_entries.values():
                if entry.entity_id == entity_id:
                    return vd.sn
        return None

    def get_virtual_device_by_entity(self, entity_id: str) -> Optional[VirtualDevice]:
        """根据 entity_id 查找其所属 VirtualDevice"""
        for vd in self.sn_virtual_device_map.values():
            for entry in vd.service_entries.values():
                if entry.entity_id == entity_id:
                    return vd
        return None

    def get_all_devices(self) -> list:
        """获取所有注册的 VirtualDevice"""
        return list(self.sn_virtual_device_map.values())

    def get_device_count(self) -> int:
        """获取已注册的设备数量"""
        return len(self.sn_virtual_device_map)

    def clear(self):
        """清空所有注册"""
        self.sn_virtual_device_map.clear()
        self.device_virtual_device_map.clear()
