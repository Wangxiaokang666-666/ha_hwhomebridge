"""用户管理的身份边界、原生状态共享和六档卡片真实业务行为。"""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from framework_imports import COMPONENT, load
from test_role_timer_composition import DeviceTransport

management = load('gateway_management')
capabilities = load('capability_router')
configuration = load('openharmony.configuration')


def example():
    value = json.loads((COMPONENT / 'openharmony/config/instances.example.json').read_text())
    value.pop('ha')
    value['devices'][0].update(id='virtual-device', layout='switches',
        cards=['fan-controls-0', 'fan-controls-1'])
    return value


class CompactRouter(capabilities.CapabilityRouter):
    registry_class = configuration.OpenHarmonyRegistry


def compact():
    value = example()
    transport = DeviceTransport(value)
    router = asyncio.run(CompactRouter.create(transport, COMPONENT / 'openharmony/config', value,
        owned_entities={'virtual-device': set(value['devices'][0]['roles'].values())},
        supported_pids={'2151'}))
    router.settle_seconds = router.fan_settle_seconds = router.confirm_timeout = 0
    return router, transport, value['devices'][0]['roles']['fan']


class CompactCardsTests(unittest.TestCase):
    def test_management_preview_uses_vendor_overrides_for_the_original_timer_product(self):
        model = management.GatewayManagement()
        model.registry = configuration.OpenHarmonyRegistry()
        asyncio.run(model.registry.load_configuration(COMPONENT / 'openharmony/config'))
        model.matcher = load('product_matcher').ProductMatcher(model.registry)
        row = example()['devices'][0]
        row['cards'] = ['timer-controls']
        products = {card['key']: product for card, product in model.card_products(row)}
        mapping = products['timer-controls'].services['fanMode'].ha_mapping
        self.assertEqual(mapping.value_attr, 'percentage')
        self.assertEqual(mapping.value_mapping.mapping_dict, {'2': 100})

    def test_every_visible_button_has_a_real_action_and_no_extra_gear(self):
        router, transport, fan = compact()
        self.assertEqual(set(router.devices), {'fan-controls-0', 'fan-controls-1'})
        self.assertFalse(transport.calls)
        expected = [('fan-controls-0', f'switch{i+1}', percentage)
                    for i, percentage in enumerate([25,37,50,62])]
        expected += [('fan-controls-1','switch1',75),('fan-controls-1','switch2',87),('fan-controls-1','switch3',100)]
        for key,sid,percentage in expected:
            before=len(transport.calls)
            router.write(key,sid,{'on':1})
            self.assertEqual(len(transport.calls),before+1)
            self.assertEqual(transport.data[fan]['attributes']['percentage'],percentage)
            for other_key,device in router.devices.items():
                for other_sid in device.product.services:
                    if other_sid=='switch' or other_key=='fan-controls-1' and other_sid=='switch4':continue
                    self.assertEqual(router.snapshot(other_key)[other_sid]['on'], int((other_key,other_sid)==(key,sid)))
        for key in router.devices:
            router.write(key,'switch',{'on':0})
            self.assertEqual(transport.data[fan]['state'],'off')
        router.write('fan-controls-1','switch4',{'on':1})
        self.assertEqual(transport.calls[-1][1],'turn_on')
        self.assertEqual(transport.data[fan]['state'],'on')
        transport.data[fan]['attributes']['percentage']=0
        router.write('fan-controls-1','switch4',{'on':1})
        self.assertEqual(transport.data[fan]['attributes']['percentage'],25)

    def test_turning_off_an_unselected_gear_does_not_stop_another_gear(self):
        router,transport,fan=compact()
        router.write('fan-controls-0','switch2',{'on':1})
        transport.calls.clear()
        router.write('fan-controls-0','switch1',{'on':0})
        self.assertEqual(transport.calls,[])
        self.assertEqual(transport.data[fan]['attributes']['percentage'],37)
        router.write('fan-controls-0','switch2',{'on':0})
        self.assertEqual(transport.data[fan]['state'],'off')

    def test_stale_percentage_while_off_does_not_show_a_selected_gear(self):
        router,transport,fan=compact()
        transport.data[fan].update(state='off')
        transport.data[fan]['attributes']['percentage']=37
        self.assertEqual(router.snapshot('fan-controls-0')['switch2']['on'],0)

    def test_reopening_and_reordering_selection_keeps_child_SNs(self):
        first,_,_=compact()
        second,_,_=compact()
        self.assertEqual({key:device.sn for key,device in first.devices.items()},
                         {key:device.sn for key,device in second.devices.items()})
        self.assertEqual(first.parent_identity,second.parent_identity)

    def test_invalid_card_selection_is_rejected_without_a_service_call(self):
        data=example();data['devices'][0]['cards']=['made-up-card']
        transport=DeviceTransport(data)
        with self.assertRaises(ValueError):
            asyncio.run(CompactRouter.create(transport,COMPONENT/'openharmony/config',data,
                owned_entities={'virtual-device':set(data['devices'][0]['roles'].values())},supported_pids={'2151'}))
        self.assertEqual(transport.calls,[])


class ManagementStorageTests(unittest.TestCase):
    def test_unpaired_previous_candidate_is_not_retained_or_registered(self):
        with tempfile.TemporaryDirectory() as name:
            runtime = Path(name)
            runtime.chmod(0o700)
            active = runtime / 'managed'
            active.mkdir(mode=0o700)
            row = {'key': 'unpaired', 'pid': 'unknown', 'sn': 'virtual-child'}
            catalog = active / 'ha-children.local.json'
            catalog.write_text(json.dumps([row]))
            catalog.chmod(0o600)
            spec = SimpleNamespace(runtime=runtime, root={'sn': 'parent'}, catalog=[], settings={'profiles': {}})
            self.assertEqual(management.retained_cards(spec, [], SimpleNamespace(definitions={})), [])
            state = active / 'ha-child-unpaired.json'
            state.write_text(json.dumps({'sn': 'virtual-child', 'parent_sn': 'another', 'devId': 'bound'}))
            state.chmod(0o600)
            with self.assertRaises(ValueError):
                management.retained_cards(spec, [], SimpleNamespace(definitions={}))

    def test_active_runtime_shares_original_KV_and_preserves_frozen_files(self):
        with tempfile.TemporaryDirectory() as name:
            runtime=Path(name);runtime.chmod(0o700);state=runtime/'sdk-state';state.mkdir(mode=0o700)
            (state/'root').write_text('original paired bytes')
            frozen=runtime/'ha-children.local.json';frozen.write_text('frozen catalog')
            sn='OHPOC-HA-virtual-child';parent='OHPOC-TESTROOT'
            child={'version':1,'parent_sn':parent,'sn':sn,'udid':'virtual-udid','devId':'cloud-original','on':0}
            path=runtime/'ha-child-old.json';path.write_text(json.dumps(child));path.chmod(0o600)
            spec=SimpleNamespace(runtime=runtime,root={'sn':parent})
            active=management.prepare_runtime(spec,[{'key':'old','sn':sn}])
            self.assertEqual((active/'sdk-state').resolve(),state.resolve())
            self.assertEqual(json.loads((active/'ha-child-old.json').read_text()),child)
            self.assertEqual(frozen.read_text(),'frozen catalog')
            child['on']=1;(active/'ha-child-old.json').write_text(json.dumps(child));(active/'ha-child-old.json').chmod(0o600)
            management.prepare_runtime(spec,[{'key':'old','sn':sn}])
            self.assertEqual(json.loads((active/'ha-child-old.json').read_text())['on'],1)
            self.assertEqual(json.loads(path.read_text())['on'],0)
            self.assertEqual((state/'root').read_text(),'original paired bytes')

    def test_foreign_state_link_and_changed_child_identity_are_rejected(self):
        with tempfile.TemporaryDirectory() as name:
            runtime=Path(name);runtime.chmod(0o700);(runtime/'sdk-state').mkdir(mode=0o700)
            active=runtime/'managed';active.mkdir(mode=0o700);(active/'sdk-state').symlink_to('/tmp')
            spec=SimpleNamespace(runtime=runtime,root={'sn':'OHPOC-TEST'})
            with self.assertRaises(ValueError):management.prepare_runtime(spec,[])
            (active/'sdk-state').unlink()
            state=active/'ha-child-old.json';state.write_text(json.dumps({'sn':'another','parent_sn':'OHPOC-TEST'}));state.chmod(0o600)
            with self.assertRaises(ValueError):management.prepare_runtime(spec,[{'key':'old','sn':'expected'}])

    def test_invalid_catalog_cannot_copy_partial_state_or_escape_runtime(self):
        with tempfile.TemporaryDirectory() as name:
            runtime = Path(name)
            runtime.chmod(0o700)
            (runtime / 'sdk-state').mkdir(mode=0o700)
            state = runtime / 'ha-child-good.json'
            state.write_text(json.dumps({'sn': 'good', 'parent_sn': 'parent'}))
            state.chmod(0o600)
            spec = SimpleNamespace(runtime=runtime, root={'sn': 'parent'})
            for catalog in ([{'key': '../escape', 'sn': 'x'}],
                            [{'key': 'good', 'sn': 'good'}, {'key': 'good', 'sn': 'duplicate'}],
                            [{'key': 'good', 'sn': 'wrong'}]):
                with self.assertRaises(ValueError):
                    management.prepare_runtime(spec, catalog)
                self.assertFalse((runtime / 'managed').exists())

    def test_management_document_cannot_replace_parent_or_original_namespace(self):
        model=management.GatewayManagement()
        model.spec=SimpleNamespace(identity='one-parent')
        model.old_sources={'original-light'}
        model.base_sources={'original-light':{'key_prefix':''}}
        good={'version':1,'identity':'one-parent','enabled':['original-light'],
              'devices':[{'kind':'composition','id':'original-light','roles':{'main':'light.fixture'}}]}
        self.assertEqual(model.document(good),good)
        for change in [{'identity':'another-parent'},{'enabled':['unknown']},
                       {'devices':[{**good['devices'][0],'key_prefix':'renamed-'}]},
                       {'devices':[{**good['devices'][0],'kind':'light'}]}]:
            with self.assertRaises(ValueError):model.document({**good,**change})


if __name__=='__main__':unittest.main()
