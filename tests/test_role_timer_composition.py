"""公共能力集成回归：虚构产品与实体，无实验配置、SDK或家庭HA依赖。"""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from framework_imports import COMPONENT, load
from test_fan_capabilities import RecordingDispatcher


capabilities = load("capability_router")
registries = load("capability_registry")
bindings = load("role_binding")
timers = load("native_timer")
legacy = load("service_router")
devices = load("virtual_device")
matchers = load("product_matcher")
transports = load("ha_transport")
FIXTURE = Path(__file__).parent / "fixtures/capabilities"
SUPPORTED = {"fixture-fan", "fixture-light"}


def instance(device="alpha", prefix=""):
    roles = {"fan": f"fan.{device}", "main": f"light.{device}", "ambient": f"light.{device}_ambient"}
    for target in ("fan", "light"):
        roles.update({f"{target}_timer_enable": f"switch.{device}_{target}_timer",
                      f"{target}_timer_duration": f"number.{device}_{target}_duration",
                      f"{target}_timer_remaining": f"sensor.{device}_{target}_remaining"})
    return {"version": 1, "parent_sn": "existing-test-parent", "devices": [
        {"id": device, "model": "fixture.fan.six", "roles": roles, "key_prefix": prefix}]}


class DeviceTransport:
    """假设备自己保存和推进原生定时，路由重建不重置此状态。"""
    def __init__(self, configuration):
        self.calls = []
        self.allowed = frozenset()
        self.data = {}
        self.timer_roles = []
        self.ignore_duration = False
        for source in configuration["devices"]:
            roles = source["roles"]
            for role, entity in roles.items():
                value = "on" if role in ("fan", "main") else "0" if entity.startswith(("number.", "sensor.")) else "off"
                attributes = {"percentage": 37, "direction": "forward"} if role == "fan" else {
                    "brightness": 128, "color_temp_kelvin": 4000, "effect": None} if role == "main" else {}
                self.data[entity] = {"entity_id": entity, "state": value, "attributes": attributes,
                                     "last_updated": datetime.now(timezone.utc).isoformat()}
            for target in ("fan", "light"):
                self.timer_roles.append(tuple(roles[f"{target}_timer_{field}"] for field in ("enable", "duration", "remaining")))

    def bind(self, entities):
        if not set(entities) <= set(self.data):
            raise ValueError("entity missing from transport")
        self.allowed = frozenset(entities)

    def states(self):
        return self.data

    def call(self, domain, action, entity, **values):
        if entity not in self.allowed or not entity.startswith(domain + "."):
            raise ValueError("out of scope call")
        self.calls.append((domain, action, entity, values))
        state = self.data[entity]
        if action == "set_value":
            if not self.ignore_duration:
                state["state"] = str(values["value"])
        elif action == "set_direction":
            state["attributes"]["direction"] = values["direction"]
        elif action == "set_percentage":
            state["state"] = "on" if values["percentage"] else "off"
            state["attributes"]["percentage"] = values["percentage"]
        elif action in ("turn_on", "turn_off"):
            state["state"] = "on" if action == "turn_on" else "off"
            state["attributes"].update(values)
            for enable, duration, remaining in self.timer_roles:
                if entity == enable:
                    self.data[remaining]["state"] = self.data[duration]["state"] if action == "turn_on" else "0"
                    self.data[remaining]["last_updated"] = datetime.now(timezone.utc).isoformat()

    def advance_device_timer(self, amount):
        for enable, _, remaining in self.timer_roles:
            if self.data[enable]["state"] == "on":
                value = max(0, float(self.data[remaining]["state"]) - amount)
                self.data[remaining]["state"] = str(value)
                self.data[remaining]["last_updated"] = datetime.now(timezone.utc).isoformat()
                if not value:
                    self.data[enable]["state"] = "off"


def make_router(configuration=None, config=FIXTURE, transport=None, manager=None, supported=SUPPORTED, inventory=None):
    configuration = copy.deepcopy(configuration or instance())
    transport = transport or DeviceTransport(configuration)
    inventory = inventory if inventory is not None else {source["id"]: set(source["roles"].values()) for source in configuration["devices"]}
    router = asyncio.run(capabilities.CapabilityRouter.create(
        transport, config, configuration, owned_entities=inventory, supported_pids=supported, sn_manager=manager))
    router.settle_seconds = router.fan_settle_seconds = 0
    router.confirm_timeout = 0
    return router, transport, configuration


class PublicRoleCompositionTests(unittest.TestCase):
    def test_preserved_primary_explicitly_shares_an_equivalent_plain_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'mapping'
            shutil.copytree(FIXTURE, config)
            path = config / 'compositions.json'
            data = json.loads(path.read_text())
            data['bundles']['fan_light'][0]['primary_role'] = 'main'
            # 额外风扇卡上的开关临时声明为主灯开关；其他风扇功能仍保留。
            data['bundles']['fan_light'][1].update(
                services={'switch': {'role': 'main', 'domain': 'light', 'action': 'turn_on_off'}},
                share_when_primary_preserved=['switch'])
            path.write_text(json.dumps(data))
            old = legacy.ServiceRouter()
            asyncio.run(old.load_product_registry(str(COMPONENT / 'config/product_registry.json')))
            primary = old.register_device('alpha', 'fixture bedside',
                [{'entity_id': 'light.alpha', 'domain': 'light'}], model='bslamp')
            router, _, _ = make_router(config=config, manager=old.sn_manager)
            self.assertIs(router.preserved_cards['light'], primary)
            self.assertIn('switch', router.devices['fan'].product.services)
            router.write('fan', 'switch', {'on': 1})
            self.assertIn('switch', primary.product.services)
            # 没有旧主卡时同样保留完整 profile 服务。
            fresh, _, _ = make_router(config=config)
            self.assertIn('switch', fresh.devices['fan'].product.services)

    def test_primary_role_mismatch_and_unsafe_sharing_reject_without_mutation(self):
        for wrong in ('primary_role', 'non_binary', 'side_effect'):
            with self.subTest(wrong=wrong), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / 'mapping'; shutil.copytree(FIXTURE, config)
                path = config / 'compositions.json'; data = json.loads(path.read_text())
                data['bundles']['fan_light'][0]['primary_role'] = 'ambient' if wrong == 'primary_role' else 'main'
                if wrong == 'non_binary':
                    data['bundles']['fan_light'][1]['share_when_primary_preserved'] = ['direction']
                if wrong == 'side_effect':
                    data['bundles']['fan_light'][1].update(
                        services={'switch': {'role': 'main', 'domain': 'light', 'action': 'turn_on_off',
                                             'options': {'power_on': {'attribute': 'brightness', 'maximum': 1, 'values': {'brightness': 100}}}}},
                        share_when_primary_preserved=['switch'])
                path.write_text(json.dumps(data))
                old = legacy.ServiceRouter()
                asyncio.run(old.load_product_registry(str(COMPONENT / 'config/product_registry.json')))
                primary = old.register_device('alpha', 'fixture bedside',
                    [{'entity_id': 'light.alpha', 'domain': 'light'}], model='bslamp')
                with self.assertRaises(ValueError):
                    make_router(config=config, manager=old.sn_manager)
                self.assertEqual(old.sn_manager.get_device_count(), 1)
                self.assertIs(old.sn_manager.get_by_sn(primary.sn), primary)

    def test_unified_catalog_preserves_normal_discovery_and_requires_explicit_composition_matching(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "capabilities"
            shutil.copytree(FIXTURE, config)
            shutil.copytree(COMPONENT / "config/adapters", config / "adapters", dirs_exist_ok=True)
            old = json.loads((COMPONENT / "config/product_registry.json").read_text())
            current = json.loads((config / "product_registry.json").read_text())
            current["products"] = old["products"] + current["products"]
            (config / "product_registry.json").write_text(json.dumps(current))
            registry = registries.CapabilityRegistry()
            asyncio.run(registry.load_configuration(config))
            matcher = matchers.ProductMatcher(registry)
            info = matchers.DeviceInfo("alpha", name="fixture light", model="fixture.fan.six", entity_domains=["light", "fan"])
            self.assertEqual(matcher.match(info).pid, "002")
            selected, _ = matcher.match_with_detail(info, include_compositions=True)
            self.assertEqual(selected.pid, "fixture-fan")
            self.assertEqual(selected.composition, "fan_light")
            router = legacy.ServiceRouter()
            router.registry = registry
            router.matcher = matcher
            vd = router.register_device("alpha", "fixture light", [{"entity_id": "light.alpha", "domain": "light"}], model="fixture.fan.six")
            self.assertEqual(vd.product.pid, "002")
            combined, _, _ = make_router(config=config, manager=router.sn_manager)
            self.assertIs(combined.preserved_cards["light"], vd)

    def test_roles_are_copied_immutable_and_checked_against_device_inventory(self):
        roles = {"fan": "fan.alpha"}
        result = bindings.RoleBindings("alpha", roles, {"fan.alpha"})
        roles["fan"] = "fan.foreign"
        self.assertEqual(result.entity("fan", "fan"), "fan.alpha")
        with self.assertRaises(TypeError):
            result.roles["fan"] = "fan.foreign"
        with self.assertRaises(ValueError):
            result.entity("fan", "light")
        for invalid in ({"fan": "fan.foreign"}, {"fan": "fan.alpha", "other": "fan.alpha"}, {"bad role": "fan.alpha"}):
            with self.assertRaises(ValueError):
                bindings.RoleBindings("alpha", invalid, {"fan.alpha"})

    def test_public_router_has_one_parent_and_stable_children_without_experimental_prefix(self):
        router, _, config = make_router()
        second, _, _ = make_router(config)
        self.assertIsInstance(router, legacy.ServiceRouter)
        self.assertEqual({device.parent_identity for device in router.devices.values()}, {config["parent_sn"]})
        self.assertEqual({key: device.sn for key, device in router.devices.items()}, {key: device.sn for key, device in second.devices.items()})
        self.assertEqual(router.devices["light"].sn, devices.SNManager.generate_sn("alpha"))
        self.assertTrue(all(device.sn.startswith("HB") for device in router.devices.values()))
        self.assertEqual(router.sn_manager.get_device_count(), 2)

    def test_second_physical_device_gets_its_own_bindings_and_stable_children(self):
        config = instance()
        config["devices"] += instance("beta", "second-")["devices"]
        router, transport, _ = make_router(config)
        router.write("second-fan", "fan", {"gear": 6})
        self.assertEqual(transport.calls[-1], ("fan", "set_percentage", "fan.beta", {"percentage": 87}))
        self.assertEqual(len({device.sn for device in router.devices.values()}), 4)
        self.assertEqual({device.parent_identity for device in router.devices.values()}, {config["parent_sn"]})

    def test_unverified_native_products_do_not_publish_any_routes(self):
        manager = devices.SNManager()
        with self.assertRaisesRegex(ValueError, "verified this PID"):
            make_router(manager=manager, supported={"fixture-light"})
        self.assertEqual(manager.get_device_count(), 0)

    def test_invalid_later_source_does_not_publish_earlier_planned_cards(self):
        config = instance()
        config["devices"] += instance("beta", "second-")["devices"]
        manager = devices.SNManager()
        owned = {source["id"]: set(source["roles"].values()) for source in config["devices"]}
        config["devices"][1]["roles"]["fan"] = "fan.foreign"
        with self.assertRaisesRegex(ValueError, "inventory"):
            make_router(config, manager=manager, inventory=owned)
        self.assertEqual(manager.get_device_count(), 0)

    def test_existing_primary_light_sn_pid_services_and_control_are_preserved(self):
        old = legacy.ServiceRouter()
        asyncio.run(old.load_product_registry(str(COMPONENT / "config/product_registry.json")))
        primary = old.register_device("alpha", "fixture bedside", [{"entity_id": "light.alpha", "domain": "light"}], model="bslamp")
        previous_product = primary.product
        previous_services = dict(primary.service_entries)
        router, transport, _ = make_router(manager=old.sn_manager)
        self.assertIs(router.preserved_cards["light"], primary)
        self.assertIs(primary.product, previous_product)
        self.assertEqual(primary.product.pid, "001")
        self.assertEqual(primary.sn, devices.SNManager.generate_sn("alpha"))
        self.assertEqual(primary.service_entries, previous_services)
        self.assertEqual(set(router.devices), {"fan"})
        self.assertEqual(router.sn_manager.get_device_count(), 2)
        router.set_hass(SimpleNamespace(states=SimpleNamespace(get=lambda entity: SimpleNamespace(**{
            key: value for key, value in transport.data[entity].items() if key in ("state", "attributes")}))))
        router.action_dispatcher = RecordingDispatcher()
        self.assertTrue(router.route_action(primary.sn, {"brightness": 50}))
        self.assertEqual(router.action_dispatcher.calls[-1], ("light", "turn_on", "light.alpha", {"brightness": 128}))
        self.assertEqual(json.loads(router.get_char_state(primary.sn, "cct")), {"colorTemperature": 4000})
        router.write("fan", "fan", {"gear": 2})
        self.assertTrue(all(call[2] == "fan.alpha" for call in transport.calls))

    def test_parent_switch_is_rejected_on_shared_manager(self):
        first, _, config = make_router()
        config["parent_sn"] = "a-second-parent"
        count = first.sn_manager.get_device_count()
        with self.assertRaisesRegex(ValueError, "second parent"):
            make_router(config, manager=first.sn_manager)
        self.assertEqual(first.sn_manager.get_device_count(), count)

    def test_same_on_field_requires_explicit_service_and_routes_ambient_separately(self):
        router, transport, _ = make_router()
        sn = router.devices["light"].sn
        with self.assertRaises(ValueError):
            router.route_action(sn, {"on": 0})
        router.route_action(sn, {"sid": "ambient", "data": {"on": 1}})
        self.assertEqual(transport.calls[-1][2], "light.alpha_ambient")
        router.route_action(sn, {"sid": "effect", "data": {"on": 1}})
        self.assertEqual(transport.calls[-1][3], {"effect": "reading"})
        self.assertEqual(router.snapshot("light")["effect"], {"on": 1})

    def test_invalid_composition_cannot_invent_profile_services_or_replace_definitions(self):
        invalid = [lambda cards: cards.append(copy.deepcopy(cards[0])),
                   lambda cards: cards[1].update(pid="unknown"),
                   lambda cards: cards[1].update(services={"invented": {"role": "fan"}}),
                   lambda cards: cards[1].update(services={"fan": {"char_name": "unregistered"}})]
        for mutate in invalid:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                config = Path(directory) / "capabilities"
                shutil.copytree(FIXTURE, config)
                data = json.loads((config / "compositions.json").read_text())
                mutate(data["bundles"]["fan_light"])
                (config / "compositions.json").write_text(json.dumps(data))
                manager = devices.SNManager()
                with self.assertRaises(ValueError):
                    make_router(config=config, manager=manager)
                self.assertEqual(manager.get_device_count(), 0)

    def test_unmarked_primary_duplicate_fails_instead_of_republishing_lights(self):
        old = legacy.ServiceRouter()
        asyncio.run(old.load_product_registry(str(COMPONENT / "config/product_registry.json")))
        primary = old.register_device("alpha", "fixture bulb", [{"entity_id": "light.alpha", "domain": "light"}], model="bslamp")
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "capabilities"
            shutil.copytree(FIXTURE, config)
            data = json.loads((config / "compositions.json").read_text())
            data["bundles"]["fan_light"][0].pop("primary")
            (config / "compositions.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "duplicates an existing primary"):
                make_router(config=config, manager=old.sn_manager)
        self.assertEqual(old.sn_manager.get_all_devices(), [primary])


class PublicNativeTimerTests(unittest.TestCase):
    def setUp(self):
        self.router, self.transport, self.config = make_router()
        self.roles = self.config["devices"][0]["roles"]

    def test_duration_is_confirmed_before_enabling_native_timer(self):
        state = self.router.write("fan", "minutes", {"time": 30})
        self.assertEqual(state["minutes"], {"time": 30})
        self.assertEqual(self.transport.calls, [
            ("number", "set_value", self.roles["fan_timer_duration"], {"value": 30}),
            ("switch", "turn_on", self.roles["fan_timer_enable"], {})])
        self.assertEqual(self.transport.data[self.roles["light_timer_enable"]]["state"], "off")

    def test_unconfirmed_duration_never_enables_the_device(self):
        self.transport.ignore_duration = True
        with self.assertRaises(TimeoutError):
            self.router.write("fan", "minutes", {"time": 30})
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.transport.calls[0][1], "set_value")
        self.assertEqual(self.transport.data[self.roles["fan_timer_enable"]]["state"], "off")

    def test_restart_reconstructs_device_timer_without_any_ha_writes(self):
        self.router.write("fan", "minutes", {"time": 30})
        self.transport.advance_device_timer(7)
        self.transport.calls.clear()
        rebuilt, _, _ = make_router(self.config, transport=self.transport, manager=self.router.sn_manager)
        self.assertEqual(rebuilt.devices["fan"].sn, self.router.devices["fan"].sn)
        self.assertEqual(rebuilt.snapshot("fan")["minutes"], {"time": 23})
        self.assertEqual(rebuilt.snapshot("fan")["countdown"]["target"], 2)
        self.assertEqual(self.transport.calls, [])
        self.transport.advance_device_timer(23)
        self.assertEqual(rebuilt.snapshot("fan")["minutes"], {"time": 0})
        self.assertEqual(self.transport.calls, [])

    def test_remaining_entity_event_reports_all_dependent_services_without_writing_device(self):
        self.router.write("fan", "minutes", {"time": 30})
        self.transport.advance_device_timer(7)
        self.transport.calls.clear()
        self.router._update_hw_bridge_status = Mock()
        device = self.router.devices["fan"]
        self.assertNotIn(self.roles["fan_timer_remaining"], {entry.entity_id for entry in device.service_entries.values()})
        self.assertTrue(self.router.report_state(device.sn, self.roles["fan_timer_remaining"], None))
        reports = self.router._update_hw_bridge_status.call_args_list
        self.assertTrue(any(args.args == (device.sn, "minutes", {"time": 23}) for args in reports))
        self.assertEqual(self.transport.calls, [])

    def test_unavailable_timer_event_does_not_report_a_false_zero(self):
        self.router.write("fan", "minutes", {"time": 30})
        self.transport.data[self.roles["fan_timer_remaining"]]["state"] = "unavailable"
        self.router._update_hw_bridge_status = Mock()
        self.assertFalse(self.router.report_state(self.router.devices["fan"].sn, self.roles["fan_timer_remaining"], None))
        self.router._update_hw_bridge_status.assert_not_called()

    def test_combined_targets_preflight_all_permissions_before_writes(self):
        self.router.set_write_entities({self.roles[role] for role in ("fan", "fan_timer_enable", "fan_timer_duration")})
        for data in ({"target": 0, "minutes": 30, "enabled": 1}, {"target": 1}):
            with self.assertRaises(ValueError):
                self.router.write("fan", "countdown", data)
        self.assertEqual(self.transport.calls, [])
        self.assertEqual(self.router.forms, {})
        self.router.write("fan", "countdown", {"target": 2, "minutes": 30, "enabled": 1})
        self.assertEqual(len(self.transport.calls), 2)

    def test_combined_targets_preflight_later_number_constraint_before_first_write(self):
        self.transport.data[self.roles["fan_timer_duration"]]["attributes"]["max"] = 10
        with self.assertRaises(ValueError):
            self.router.write("fan", "countdown", {"target": 0, "minutes": 30, "enabled": 1})
        self.assertEqual(self.transport.calls, [])

    def test_fan_and_light_timers_remain_independent(self):
        self.router.write("fan", "countdown", {"target": 1, "minutes": 15, "enabled": 1})
        self.router.write("fan", "countdown", {"target": 2, "minutes": 45, "enabled": 1})
        self.router.write("fan", "minutes", {"time": 0})
        state = self.router.snapshot("fan")["countdown"]
        self.assertEqual(self.transport.data[self.roles["light_timer_remaining"]]["state"], "15")
        self.assertEqual(self.router.snapshot("fan")["minutes"], {"time": 0})
        self.assertEqual(state["enabled"], 0)  # 表单仍选风扇，不冒充灯定时被取消。

    def test_timer_array_uses_native_remaining_timestamp_and_can_cancel(self):
        end = (datetime.now(timezone.utc) + timedelta(minutes=8)).strftime("%Y%m%dT%H%M%SZ")
        self.router.write("fan", "delay", {"action": 1, "num": 1, "timer": [{"id": 1, "enable": 1, "end": end}]})
        self.assertEqual(self.router.snapshot("fan")["delay"]["num"], 1)
        self.router.write("fan", "delay", {"action": 2, "num": 1, "timer": [{"id": 1}]})
        self.assertEqual(self.router.snapshot("fan")["minutes"], {"time": 0})

    def test_compound_timer_services_do_not_require_one_primary_characteristic(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "capabilities"
            shutil.copytree(FIXTURE, config)
            data = json.loads((config / "product_registry.json").read_text())
            for sid in ("delay", "countdown"):
                data["products"][0]["services"][sid].pop("char_name")
            (config / "product_registry.json").write_text(json.dumps(data))
            router, _, _ = make_router(config=config)
            router.write("fan", "countdown", {"target": 2, "minutes": 8, "enabled": 1})
            self.assertEqual(router.snapshot("fan")["delay"]["num"], 1)
            router.write("fan", "delay", {"action": 2, "timer": [{"id": 1}]})
            self.assertEqual(router.snapshot("fan")["minutes"], {"time": 0})

    def test_invalid_native_timer_commands_never_call_transport(self):
        end = (datetime.now(timezone.utc) + timedelta(minutes=8)).strftime("%Y%m%dT%H%M%SZ")
        invalid = [("minutes", {"time": -1}), ("minutes", {"time": True}), ("minutes", {"time": 721}),
                   ("countdown", {"remaining": 1}), ("countdown", {"target": 3, "enabled": 1}),
                   ("delay", {"action": 1, "num": 0, "timer": [{"end": end}]}),
                   ("delay", {"action": 1, "timer": [{"end": end, "sid": "foreign"}]}),
                   ("delay", {"action": 2, "timer": [{"id": 2}]}),
                   ("delay", {"action": 1, "timer": [{"end": "20000101T000000Z"}]}),
                   ("delay", {"action": 1, "timer": [{"end": end, "paraValue": True}]})]
        for sid, data in invalid:
            with self.subTest(sid=sid, data=data), self.assertRaises(ValueError):
                self.router.write("fan", sid, data)
        self.assertEqual(self.transport.calls, [])

    def test_unavailable_sensor_invalid_step_and_read_only_reject_before_calls(self):
        remaining = self.transport.data[self.roles["fan_timer_remaining"]]
        for value in ("unavailable", "nan", "-1"):
            remaining["state"] = value
            with self.assertRaises(ValueError):
                self.router.write("fan", "minutes", {"time": 30})
        remaining["state"] = "0"
        self.transport.data[self.roles["fan_timer_duration"]]["attributes"]["step"] = 7
        with self.assertRaises(ValueError):
            self.router.write("fan", "minutes", {"time": 30})
        self.router.set_write_entities(set())
        with self.assertRaises(ValueError):
            self.router.write("fan", "minutes", {"time": 0})
        self.assertEqual(self.transport.calls, [])

    def test_inactive_cancel_is_noop_even_when_remaining_sensor_is_unavailable(self):
        self.transport.data[self.roles["fan_timer_remaining"]]["state"] = "unavailable"
        self.router.write("fan", "minutes", {"time": 0})
        self.assertEqual(self.transport.calls, [])

    def test_seconds_units_are_explicit_and_read_back_as_minutes(self):
        role_map = bindings.RoleBindings("alpha", self.roles, self.roles.values())
        controller = timers.NativeTimerController(role_map, self.transport, lambda entities: None,
            lambda predicate: self.assertTrue(predicate()))
        timer = timers.TimerBinding.parse({"enable": "fan_timer_enable", "duration": "fan_timer_duration",
            "remaining": "fan_timer_remaining", "maximum": 720, "duration_unit": "seconds", "remaining_unit": "seconds"})
        controller.set(timer, 3)
        self.assertEqual(self.transport.calls[0][3], {"value": 180})
        self.assertEqual(controller.remaining(timer), 3)
        self.transport.data[self.roles["fan_timer_remaining"]]["state"] = "61"
        self.assertEqual(controller.remaining(timer), 2)

    def test_timer_role_domains_must_be_valid_before_composition_publication(self):
        config = instance()
        config["devices"][0]["roles"]["fan_timer_enable"] = "number.wrong_domain"
        manager = devices.SNManager()
        with self.assertRaisesRegex(ValueError, "domain"):
            make_router(config, manager=manager)
        self.assertEqual(manager.get_device_count(), 0)


class LocalHATransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_worker_calls_are_scoped_and_wait_for_ha_completion(self):
        records = []
        state = SimpleNamespace(as_dict=lambda: {"entity_id": "fan.alpha", "state": "on", "attributes": {}})
        async def call(domain, action, values, blocking):
            self.assertTrue(blocking)
            await asyncio.sleep(0)
            records.append((domain, action, values))
        hass = SimpleNamespace(loop=asyncio.get_running_loop(), states=SimpleNamespace(async_all=lambda: [state]),
                               services=SimpleNamespace(async_call=call))
        transport = transports.HomeAssistantTransport(hass)
        transport.bind({"fan.alpha"})
        with self.assertRaises(RuntimeError):
            transport.states()
        self.assertEqual(set(await asyncio.to_thread(transport.states)), {"fan.alpha"})
        await asyncio.to_thread(transport.call, "fan", "set_percentage", "fan.alpha", percentage=37)
        self.assertEqual(records, [("fan", "set_percentage", {"entity_id": "fan.alpha", "percentage": 37})])
        for entity, values in (("fan.foreign", {"percentage": 25}), ("fan.alpha", {"entity_id": "fan.foreign"})):
            with self.assertRaises(ValueError):
                await asyncio.to_thread(transport.call, "fan", "set_percentage", entity, **values)
        readonly = transports.HomeAssistantTransport(hass, writable=set())
        readonly.bind({"fan.alpha"})
        with self.assertRaises(ValueError):
            await asyncio.to_thread(readonly.call, "fan", "set_percentage", "fan.alpha", percentage=25)
        self.assertEqual(len(records), 1)

    async def test_ha_event_reports_use_current_loop_snapshot_without_waiting_on_itself(self):
        # create本身是异步工厂；在当前HA事件循环中直接初始化。
        config = instance()
        transport = DeviceTransport(config)
        router = await capabilities.CapabilityRouter.create(transport, FIXTURE, config,
            owned_entities={"alpha": set(config["devices"][0]["roles"].values())}, supported_pids=SUPPORTED)
        def state(entity):
            return SimpleNamespace(as_dict=lambda: transport.data[entity])
        router.set_hass(SimpleNamespace(loop=asyncio.get_running_loop(), states=SimpleNamespace(get=state)))
        transport.states = Mock(side_effect=AssertionError("cannot wait on HA loop from its callback"))
        router._update_hw_bridge_status = Mock()
        self.assertTrue(router.report_state(router.devices["fan"].sn, "sensor.alpha_fan_remaining", None))
        self.assertTrue(router._update_hw_bridge_status.called)


if __name__ == "__main__":
    unittest.main()
