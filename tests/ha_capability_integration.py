"""仅在官方HA镜像内执行：真实HA状态/服务/实体注册表，虚构设备，无SDK。"""
import asyncio
import copy
import ctypes
import json
import tempfile
from datetime import datetime, timezone
from uuid import uuid4
from unittest.mock import Mock

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry
from homeassistant.helpers import entity_registry

from framework_imports import COMPONENT, load
from test_role_timer_composition import DeviceTransport, FIXTURE, SUPPORTED, instance


capabilities = load("capability_router")
transports = load("ha_transport")
legacy = load("service_router")


async def exercise(directory):
    hass = HomeAssistant(directory)
    loader.async_setup(hass)
    device_registry.async_setup(hass)
    await device_registry.async_load(hass, load_empty=True)
    await entity_registry.async_load(hass, load_empty=True)
    registry = entity_registry.async_get(hass)
    config = instance()
    actor = DeviceTransport(config)
    roles = config["devices"][0]["roles"]
    actor.bind(roles.values())
    def entry(entity, device_id, disabled_by=None):
        # 真实RegistryEntry，测试归属数据为虚构值；不加载任何设备集成。
        return entity_registry.RegistryEntry(
            entity_id=entity, unique_id=entity, platform="fixture", device_id=device_id,
            capabilities=None, config_entry_id=None, config_subentry_id=None,
            created_at=datetime.now(timezone.utc), disabled_by=disabled_by,
            entity_category=None, has_entity_name=False, hidden_by=None, id=uuid4().hex,
            object_id_base=entity.split(".")[1], options={}, original_device_class=None,
            original_icon=None, original_name=None, suggested_object_id=None,
            supported_features=0, translation_key=None, unit_of_measurement=None)
    for entity in roles.values():
        registry.entities[entity] = entry(entity, "alpha")
    registry.entities["fan.foreign"] = entry("fan.foreign", "foreign")
    registry.entities["fan.disabled"] = entry("fan.disabled", "alpha", entity_registry.RegistryEntryDisabler.USER)
    inventory = transports.async_owned_entity_inventory(hass, {"alpha"})
    assert inventory == {"alpha": set(roles.values())}, "foreign or disabled role entered inventory"

    def sync_states():
        for entity, state in actor.data.items():
            hass.states.async_set(entity, state["state"], state["attributes"])

    sync_states()
    async def service(call):
        entity = call.data["entity_id"]
        assert entity in set(roles.values()), "foreign device service rejected"
        actor.call(call.domain, call.service, entity, **{key: value for key, value in call.data.items() if key != "entity_id"})
        sync_states()
    for domain, actions in transports.ScopedTransport.ACTIONS.items():
        for action in actions:
            hass.services.async_register(domain, action, service)

    previous = legacy.ServiceRouter()
    previous.set_hass(hass)
    assert await previous.load_product_registry(str(COMPONENT / "config/product_registry.json"))
    primary = previous.register_device("alpha", "fixture bedside", [{"entity_id": roles["main"], "domain": "light"}], model="bslamp")
    old_sn, old_product = primary.sn, primary.product
    transport = transports.HomeAssistantTransport(hass)
    router = await capabilities.CapabilityRouter.create(transport, FIXTURE, config,
        owned_entities=inventory, supported_pids=SUPPORTED, sn_manager=previous.sn_manager)
    router.set_hass(hass)
    router.settle_seconds = router.fan_settle_seconds = 0
    router.confirm_timeout = 3
    assert router.preserved_cards["light"] is primary and primary.product is old_product
    assert len(router.devices) == 1 and primary.sn == old_sn
    for gear, percentage in ((1, 25), (2, 37), (6, 87)):
        await asyncio.to_thread(router.write, "fan", "fan", {"gear": gear})
        assert hass.states.get(roles["fan"]).attributes["percentage"] == percentage
    await asyncio.to_thread(router.write, "fan", "mode", {"mode": 2})
    assert hass.states.get(roles["fan"]).attributes["percentage"] == 100
    await asyncio.to_thread(router.write, "fan", "direction", {"reverse": 1})
    assert hass.states.get(roles["fan"]).attributes["direction"] == "reverse"
    await asyncio.to_thread(router.write, "fan", "minutes", {"time": 30})
    actor.advance_device_timer(7)
    sync_states()
    count = len(actor.calls)
    rebuilt = await capabilities.CapabilityRouter.create(transport, FIXTURE, config,
        owned_entities=inventory, supported_pids=SUPPORTED, sn_manager=router.sn_manager)
    rebuilt.set_hass(hass)
    rebuilt.settle_seconds = rebuilt.fan_settle_seconds = 0
    rebuilt.confirm_timeout = 3
    snapshot = await asyncio.to_thread(rebuilt.snapshot, "fan")
    assert snapshot["minutes"] == {"time": 23} and len(actor.calls) == count
    assert rebuilt.devices["fan"].sn == router.devices["fan"].sn
    rebuilt._update_hw_bridge_status = Mock()
    assert rebuilt.report_state(primary.sn, roles["fan_timer_remaining"], hass.states.get(roles["fan_timer_remaining"]))
    assert any(item.args[1:] == ("minutes", {"time": 23}) for item in rebuilt._update_hw_bridge_status.call_args_list)

    count = len(actor.calls)
    bad = copy.deepcopy(config)
    bad["devices"][0]["roles"]["fan"] = "fan.foreign"
    try:
        await capabilities.CapabilityRouter.create(transport, FIXTURE, bad,
            owned_entities=inventory, supported_pids=SUPPORTED, sn_manager=router.sn_manager)
    except ValueError:
        pass
    else:
        raise AssertionError("foreign role accepted")
    assert len(actor.calls) == count
    rebuilt.set_write_entities({roles[role] for role in ("fan", "fan_timer_enable", "fan_timer_duration")})
    try:
        await asyncio.to_thread(rebuilt.write, "fan", "countdown", {"target": 0, "minutes": 10, "enabled": 1})
    except ValueError:
        pass
    else:
        raise AssertionError("mixed read-only timer accepted")
    assert len(actor.calls) == count
    await asyncio.to_thread(rebuilt.write, "fan", "minutes", {"time": 0})
    assert hass.states.get(roles["fan_timer_enable"]).state == "off"
    # 旧灯仍经过原ServiceActionDispatcher；不用组合卡重新注册或改PID。
    assert rebuilt.route_action(primary.sn, {"brightness": 50})
    await hass.async_block_till_done()
    assert hass.states.get(roles["main"]).attributes["brightness"] == 128
    assert json.loads(rebuilt.get_char_state(primary.sn, "cct")) == {"colorTemperature": 4000}
    result = {"passed": True, "real_HA_state_services_and_entity_registry": True,
              "fake_physical_devices": True, "registered_gateways": 0,
              "one_supplied_parent_identity": True, "old_primary_identity_and_product_preserved": True,
              "role_inventory_foreign_and_disabled_excluded": True,
              "fan_speed_mode_direction": True, "native_timer_confirm_cancel_and_reconstruction": True,
              "timer_state_event_on_HA_loop": True, "legacy_light_control_and_readback": True,
              "partial_read_only_timer_rejected_without_calls": True,
              "production_HA_or_SDK_calls": False, "hwhomebridge_config_flow_or_SDK_pairing_tested": False}
    await hass.async_stop()
    return result


def main():
    original = ctypes.CDLL
    def reject_sdk(name, *args, **kwargs):
        if any(token in str(name).lower() for token in ("hilink", "bridge_poc")):
            raise AssertionError("gateway SDK loading is forbidden")
        return original(name, *args, **kwargs)
    ctypes.CDLL = reject_sdk
    with tempfile.TemporaryDirectory(prefix="ha-capability-fixture-") as directory:
        result = asyncio.run(exercise(directory))
    print("HA_CAPABILITY_RESULT:" + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
