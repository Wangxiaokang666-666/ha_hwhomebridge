"""公共动作、查询和上报回归；使用假实体，不访问 HA 或 SDK。"""
import asyncio
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from framework_imports import COMPONENT, load


models = load("product_registry")
actions = load("service_action")
routers = load("service_router")
devices = load("virtual_device")
fans = load("fan_mapping")
matchers = load("product_matcher")


def mapping(action="set_speed", table=None, **options):
    attrs = {"set_speed": "percentage", "set_direction": "direction",
             "set_fan_mode": "percentage"}
    if action == "set_fan_mode" and options.get("ha_service") == "set_preset_mode":
        attrs[action] = "preset_mode"
    return models.ProductRegistry()._parse_ha_mapping({
        "domain": "fan", "action": action, "value_attr": attrs[action],
        "value_mapping": {"type": "enum_to_number", "mapping": table if table is not None else {
            "1": 25, "2": 37, "3": 50, "4": 62, "5": 75, "6": 87}},
        "options": options,
    })


class RecordingDispatcher(actions.ServiceActionDispatcher):
    def __init__(self):
        super().__init__(SimpleNamespace())
        self.calls = []

    def _call_service_async(self, domain, service, entity_id, data=None):
        self.calls.append((domain, service, entity_id, data or {}))


def core_router():
    router = routers.ServiceRouter()
    router.action_dispatcher = RecordingDispatcher()
    product = models.ProductDef(pid="fixture-only", name="fixture fan", services={
        "fan": models.ServiceDef("fan", "fan", char_name="gear", ha_mapping=mapping()),
        "direction": models.ServiceDef("direction", "verticalswing", char_name="reversal",
            c_service_id="verticalswing", ha_mapping=mapping("set_direction", {"0": "reverse", "1": "forward"})),
        "mode": models.ServiceDef("mode", "fanMode", char_name="mode",
            ha_mapping=mapping("set_fan_mode", {"2": 100}, unmapped=0)),
    })
    vd = devices.VirtualDevice("fixture-sn", product, "fixture-device")
    for sid in product.services:
        vd.add_service_entry(sid, devices.ServiceEntry("fan.fixture", "fan", sid))
    router.sn_manager.register(vd)
    state = SimpleNamespace(state="on", attributes={"percentage": 37, "direction": "forward"})
    router.set_hass(SimpleNamespace(states=SimpleNamespace(get=lambda entity: state)))
    router._update_hw_bridge_status = Mock()
    return router, vd, state


class FanCapabilityTests(unittest.TestCase):
    def test_six_gears_dispatch_to_native_percentages_without_legacy_scaling(self):
        dispatcher = RecordingDispatcher()
        config = mapping()
        for gear, percentage in enumerate((25, 37, 50, 62, 75, 87), 1):
            self.assertTrue(dispatcher.dispatch("fixture", "fan.fixture", config, {"gear": gear}))
            self.assertEqual(dispatcher.calls[-1], ("fan", "set_percentage", "fan.fixture", {"percentage": percentage}))
            self.assertEqual(fans.fan_state(config, "on", {"percentage": percentage}), gear)

    def test_other_fan_gear_count_needs_only_mapping_configuration(self):
        config = mapping(table={"1": 33, "2": 66, "3": 100})
        self.assertEqual(fans.fan_command(config, 3), ("set_percentage", {"percentage": 100}))
        self.assertEqual(fans.fan_state(config, "on", {"percentage": 66}), 2)

    def test_percentage_natural_wind_and_preset_modes_use_distinct_ha_services(self):
        numeric = mapping("set_fan_mode", {"2": 100}, unmapped=0)
        preset = mapping("set_fan_mode", {"1": "normal", "2": "natural"}, ha_service="set_preset_mode")
        self.assertEqual(fans.fan_command(numeric, 2), ("set_percentage", {"percentage": 100}))
        self.assertEqual(fans.fan_command(preset, 2), ("set_preset_mode", {"preset_mode": "natural"}))
        self.assertEqual(fans.fan_state(numeric, "on", {"percentage": 87}), 0)
        self.assertEqual(fans.fan_state(preset, "on", {"preset_mode": "natural"}), 2)

    def test_vendor_direction_polarity_is_bidirectional_and_not_global(self):
        vendor = mapping("set_direction", {"0": "reverse", "1": "forward"})
        standard = mapping("set_direction", {"0": "forward", "1": "reverse"})
        for value, actual in ((0, "reverse"), (1, "forward")):
            self.assertEqual(fans.fan_command(vendor, value), ("set_direction", {"direction": actual}))
            self.assertEqual(fans.fan_state(vendor, "on", {"direction": actual}), value)
        self.assertEqual(fans.fan_state(standard, "on", {"direction": "reverse"}), 1)

    def test_invalid_commands_never_schedule_ha(self):
        dispatcher = RecordingDispatcher()
        for value in (0, 7, -1, 1.5, True, "1", None, float("nan"), float("inf"), 10**1000):
            with self.subTest(value_type=type(value).__name__):
                self.assertFalse(dispatcher.dispatch("fixture", "fan.fixture", mapping(), {"gear": value}))
        for payload in ({}, {"gear": 1, "other": 2}, [1], None):
            self.assertFalse(dispatcher.dispatch("fixture", "fan.fixture", mapping(), payload))
        self.assertFalse(dispatcher.dispatch("fixture", "light.fixture", mapping(), {"gear": 1}))
        self.assertEqual(dispatcher.calls, [])

    def test_invalid_mapping_rejected_at_configuration_parse(self):
        for table in ({"1": 101}, {"1": -1}, {"1": True}, {"1": float("nan")},
                      {"1": 25, "2": 25}, {"01": 25}, {"-1": 25}, {}, []):
            with self.subTest(table=table), self.assertRaises(ValueError):
                mapping(table=table)
        with self.assertRaises(ValueError):
            mapping("set_direction", {"0": "up"})
        with self.assertRaises(ValueError):
            mapping(ha_service="turn_off")

    def test_unavailable_missing_and_unmapped_attributes_do_not_invent_a_gear(self):
        config = mapping()
        for state, attrs in (("unknown", {"percentage": 37}), ("unavailable", {"percentage": 37}),
                             ("on", {}), ("on", {"percentage": None}), ("on", {"percentage": 36}),
                             ("on", {"percentage": True}), ("on", {"percentage": float("nan")}),
                             ("on", {"percentage": "37"}), ("on", {"percentage": 101})):
            with self.subTest(state=state, attrs=attrs):
                self.assertIsNone(fans.fan_state(config, state, attrs))

    def test_three_layer_merge_keeps_vendor_options_isolated(self):
        registry = models.ProductRegistry()
        base = mapping("set_direction", {"0": "forward", "1": "reverse"})
        override = {"value_mapping": {"type": "enum_to_number", "mapping": {"0": "reverse", "1": "forward"}},
                    "options": {"unmapped": 0}}
        merged = registry._merge_ha_mapping(base, override)
        override["options"]["unmapped"] = 99
        self.assertEqual(fans.fan_state(merged, "on", {"direction": "reverse"}), 0)
        self.assertEqual(merged.options, {"unmapped": 0})
        self.assertEqual(base.options, {})
        self.assertEqual(fans.fan_state(base, "on", {"direction": "reverse"}), 1)

    def test_common_router_routes_real_characteristics_and_rejects_mixed_command(self):
        router, vd, _ = core_router()
        self.assertTrue(router.route_action(vd.sn, {"gear": 2}))
        self.assertEqual(router.action_dispatcher.calls[-1][-1], {"percentage": 37})
        self.assertTrue(router.route_action(vd.sn, {"reversal": 1}))
        self.assertEqual(router.action_dispatcher.calls[-1][-1], {"direction": "forward"})
        count = len(router.action_dispatcher.calls)
        for payload in ({"gear": 2, "reversal": 1}, {"gear": 2, "on": 1}, {"bad": 2}, {"gear": 9}):
            self.assertFalse(router.route_action(vd.sn, payload))
        self.assertEqual(len(router.action_dispatcher.calls), count)

    def test_queries_and_reports_share_actual_state_instead_of_last_command(self):
        router, vd, state = core_router()
        router.route_action(vd.sn, {"gear": 6})
        router._report_by_type(vd.sn, "fan", vd.product.services["fan"], state)
        router._update_hw_bridge_status.assert_called_once_with(vd.sn, "fan", {"gear": 2})
        self.assertEqual(json.loads(router.get_char_state(vd.sn, "fan")), {"gear": 2})
        self.assertEqual(json.loads(router.get_char_state(vd.sn, "verticalswing")), {"reversal": 1})
        state.state = "unavailable"
        router._update_hw_bridge_status.reset_mock()
        router._report_by_type(vd.sn, "fan", vd.product.services["fan"], state)
        self.assertIsNone(router.get_char_state(vd.sn, "fan"))
        router._update_hw_bridge_status.assert_not_called()

    def test_unknown_characteristic_cannot_use_single_service_fallback(self):
        router, vd, _ = core_router()
        for sid in ("direction", "mode"):
            del vd.service_entries[sid]
        self.assertFalse(router.route_action(vd.sn, {"other": 2}))
        self.assertEqual(router.action_dispatcher.calls, [])

    def test_non_object_commands_are_rejected_before_routing(self):
        router, vd, _ = core_router()
        for payload in (None, [], True, 2, "[]", "null", "true", "{}", "invalid JSON"):
            with self.subTest(payload=payload):
                self.assertFalse(router.route_action(vd.sn, payload))
        self.assertEqual(router.action_dispatcher.calls, [])

    def test_ambiguous_profile_characteristic_is_rejected(self):
        router, vd, _ = core_router()
        vd.product.services["direction"].char_name = "gear"
        self.assertFalse(router.route_action(vd.sn, {"gear": 1}))
        self.assertEqual(router.action_dispatcher.calls, [])

    def test_new_fan_field_cannot_steal_existing_light_characteristic(self):
        router, vd, _ = core_router()
        vd.product.services["fan"].char_name = "on"
        vd.product.services["switch"] = models.ServiceDef("switch", "switch", char_name="on",
            ha_mapping=models.HAMapping(domain="light", action="turn_on_off", value_attr="state"))
        vd.add_service_entry("switch", devices.ServiceEntry("light.fixture", "light", "switch"))
        self.assertFalse(router.route_action(vd.sn, {"on": 1}))
        self.assertEqual(router.action_dispatcher.calls, [])

    def test_unconfigured_legacy_speed_action_retains_previous_behavior(self):
        dispatcher = RecordingDispatcher()
        config = models.HAMapping(domain="fan", action="set_speed")
        self.assertTrue(dispatcher.dispatch("fixture", "fan.fixture", config, {"speed": 10}))
        self.assertEqual(dispatcher.calls[-1][-1], {"percentage": 40})
        self.assertFalse(fans.is_mapped_fan(config))

    def test_original_light_commands_keep_entities_and_percent_scale(self):
        router = routers.ServiceRouter()
        self.assertTrue(asyncio.run(router.load_product_registry(str(COMPONENT / "config/product_registry.json"))))
        vd = router.register_device("fixture-light", "fixture bulb", [{"entity_id": "light.fixture", "domain": "light"}], model="bslamp")
        dispatcher = RecordingDispatcher()
        router.action_dispatcher = dispatcher
        for payload in ({"on": 1}, {"brightness": 50}, {"colorTemperature": 4000}):
            self.assertTrue(router.route_action(vd.sn, payload))
        self.assertEqual(dispatcher.calls, [
            ("light", "turn_on", "light.fixture", {}),
            ("light", "turn_on", "light.fixture", {"brightness": 128}),
            ("light", "turn_on", "light.fixture", {"color_temp_kelvin": 4000}),
        ])
        state = SimpleNamespace(state="on", attributes={"brightness": 255, "color_temp_kelvin": 4000})
        for sid, expected in (("switch", {"on": 1}), ("brightness", {"brightness": 100}), ("cct", {"colorTemperature": 4000})):
            result = router._build_char_state_json(sid, vd.product.services[sid], state, "light.fixture", vd)
            self.assertEqual(json.loads(result), expected)
        self.assertEqual(vd.sn, devices.SNManager.generate_sn("fixture-light"))

    def test_light_percent_survives_vendor_integer_quantization(self):
        dispatcher = RecordingDispatcher()
        mapping = models.HAMapping(domain="light", action="set_brightness", brightness_range=100)
        for percent in range(1, 101):
            self.assertTrue(dispatcher.dispatch("fixture", "light.fixture", mapping, {"brightness": percent}))
            ha_brightness = dispatcher.calls[-1][-1]["brightness"]
            # 小米设备把 HA 亮度换回整数百分比；45% 原先被截成44%，上报再降为43%。
            device_percent = int(ha_brightness * 100 / 255)
            reported_ha = round(device_percent * 255 / 100)
            reported_percent = int(reported_ha * 100 / 255)
            self.assertEqual(device_percent, percent)
            self.assertLessEqual(abs(reported_percent - percent), 1)

    def test_all_legacy_product_matches_and_fan_light_coexistence_keep_old_pid(self):
        registry = models.ProductRegistry()
        self.assertTrue(asyncio.run(registry.load(str(COMPONENT / "config/product_registry.json"), str(COMPONENT / "config/adapters"))))
        self.assertEqual(set(registry.list_product_ids()), {f"{i:03}" for i in range(1, 9)})
        matcher = matchers.ProductMatcher(registry)
        cases = (("bslamp", "001"), ("yeelink.light.mbulb3", "002"), ("shuse.light.lgt02", "003"),
                 ("linp.senpres.ps1bb", "004"), ("xiaomi.kettle.ym3pro", "005"),
                 ("dayang.mosq.dyt16s", "006"), ("chunmi.cooker.c301", "007"), ("ykcn.valve.cbcs", "008"))
        for model, pid in cases:
            with self.subTest(model=model):
                product = matcher.match(matchers.DeviceInfo("fixture", model=model))
                self.assertEqual(product.pid, pid)
        original = matcher.match(matchers.DeviceInfo("fixture", name="书房灯", model="shhf.light.sflt11", entity_domains=["light", "fan"]))
        self.assertEqual(original.pid, "002")
        self.assertTrue(all(not fans.is_mapped_fan(s.ha_mapping) for s in original.services.values()))


class FanEventLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_callback_worker_schedules_service_on_ha_loop(self):
        calls = []
        completed = asyncio.Event()
        main_thread = threading.get_ident()

        async def call(**kwargs):
            calls.append((threading.get_ident(), kwargs))
            completed.set()

        hass = SimpleNamespace(loop=asyncio.get_running_loop(), async_create_task=asyncio.create_task,
                               services=SimpleNamespace(async_call=call))
        dispatcher = actions.ServiceActionDispatcher(hass)
        accepted = await asyncio.to_thread(dispatcher.dispatch, "fixture", "fan.fixture", mapping(), {"gear": 2})
        self.assertTrue(accepted)
        await asyncio.wait_for(completed.wait(), 1)
        self.assertEqual(calls, [(main_thread, {"domain": "fan", "service": "set_percentage",
            "target": {"entity_id": "fan.fixture"}, "service_data": {"percentage": 37}})])


if __name__ == "__main__":
    unittest.main()
