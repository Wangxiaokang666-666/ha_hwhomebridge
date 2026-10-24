"""虚构身份的冷备、只读实体归属及故障拒绝；不加载 SDK、不连接家庭 HA。"""
import asyncio
import copy
import hashlib
import hmac
import json
from pathlib import Path
import shutil
import struct
import tempfile
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import ha_cold_export as cold
import ha_entity_preflight as inventory

COMPONENT = Path(__file__).resolve().parents[1]/cold.COMPONENT
AC, MAC = bytes(range(48)), '02:11:22:33:44:55'


def snapshot():
    source, entry = 'virtual-light-source', 'original-entry'
    sn = inventory.core_module('virtual_device').SNManager.generate_sn(source)
    return {'entries':[{'entry_id':entry,'domain':'hwhomebridge','unique_id':'hwhomebridge','state':'loaded'}],
        'devices':[{'id':source,'name':'Virtual light','model':'fixture-light','config_entries':['fake-source']},
                   {'id':'old-bridge','config_entries':[entry],'identifiers':[['hwhomebridge',entry,sn]]}],
        'entities':[{'entity_id':'light.fixture','device_id':source,'platform':'fixture','unique_id':'original-unique'}],
        'states':[{'entity_id':'light.fixture','state':'on','attributes':{'brightness':128,'color_temp_kelvin':4000}}],
        'services':{'light':['turn_on','turn_off']}}


def encrypted(data, salt):
    key = hmac.digest(AC[:16]+bytes.fromhex(MAC.replace(':','')),salt,'sha256')
    encoder = Cipher(algorithms.AES(key[:16]),modes.CBC(key[16:])).encryptor()
    return encoder.update(data)+encoder.finalize()


def legacy_bytes(sn):
    codec = inventory.core_module('identity_store')
    config = bytearray(1588);struct.pack_into('<I',config,0,1552);config[36]=3
    config[44:60]=bytes(range(16))
    config[76:172]=encrypted(b'11111111-2222-4333-8444-555555555555'.ljust(40,b'\0')+b's'*32+bytes(8)+b'p'*16,config[44:60])
    config[172:236]=b'cloud.test.invalid'.ljust(64,b'\0');config[400:440]=(b'a'*32).ljust(40,b'\0')
    config[440:456]=encrypted(b'c'*16,config[44:60]);config[1244:1260]=bytes(range(16,32))
    key=codec.hkdf(AC[:16]+bytes.fromhex(MAC.replace(':','')),bytes(config[1244:1260]),b'hilink_hkdf_base_info')
    encoder=Cipher(algorithms.AES(key[:16]),modes.CBC(key[16:])).encryptor()
    config[1260:1324]=encoder.update(b'f'*64)+encoder.finalize();config[4:36]=hashlib.sha256(config[36:]).digest()
    bridge=bytearray(5192);struct.pack_into('<II',bridge,0,5136,4);bridge[40:56]=bytes(range(32,48))
    payload=(struct.pack('<I',1)+bytes(12)+b'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee'.ljust(40,b'\0')+sn.encode().ljust(40,b'\0')).ljust(5136,b'\0')
    bridge[56:]=encrypted(payload,bridge[40:56]);bridge[8:40]=hashlib.sha256(bridge[40:5176]).digest()
    return bytes(config),bytes(bridge)


class EntityPreflightTest(unittest.TestCase):
    def test_singular_storage_owner_matches_live_compatibility_list(self):
        old = snapshot()['devices']
        stored = copy.deepcopy(old)
        for row in stored:
            row['config_entry_id'] = row.pop('config_entries')[0]
        self.assertEqual(inventory.project(old, inventory.DEVICE_FIELDS, 'id'),
                         inventory.project(stored, inventory.DEVICE_FIELDS, 'id'))
        stored[0]['config_entry_id'] = 'different-owner'
        self.assertNotEqual(inventory.project(old, inventory.DEVICE_FIELDS, 'id'),
                            inventory.project(stored, inventory.DEVICE_FIELDS, 'id'))

    def test_composite_storage_is_not_guessed_as_single_owner(self):
        row = {'id': 'fixture', 'config_entry_id': 'owner', 'composite_device_id': 'composite'}
        with self.assertRaises(ValueError):
            inventory.project([row], inventory.DEVICE_FIELDS, 'id')

    def test_partial_entity_list_fetches_full_classes_only_for_selected_sources(self):
        data = snapshot()
        class Reader:
            def __init__(self): self.details = []
            async def command(self, kind, *, entity_id=None):
                if kind == 'get_config': return {'version': '2026.10.1'}
                if kind == 'config_entries/get': return data['entries']
                if kind == 'get_services': return {'light': {'turn_on': {}, 'turn_off': {}}}
                if kind == 'config/device_registry/list': return data['devices']
                if kind == 'config/entity_registry/list':
                    return data['entities'] + [{'entity_id': 'sensor.unrelated', 'device_id': 'outside'}]
                if kind == 'get_states': return data['states']
                if kind == 'config/entity_registry/get':
                    self.details.append(entity_id)
                    return {**data['entities'][0], 'device_class': None, 'original_device_class': 'fixture-class'}
                raise AssertionError('unexpected command')
        reader = Reader()
        result = asyncio.run(inventory.capture(reader))
        self.assertEqual(reader.details, ['light.fixture'])
        self.assertEqual(result['entities'][0]['original_device_class'], 'fixture-class')

    def test_detail_command_arguments_remain_closed(self):
        reader = inventory.HAReader('http://test.invalid', 'not-a-secret')
        with self.assertRaises(ValueError):
            asyncio.run(reader.command('config/entity_registry/get'))
        with self.assertRaises(ValueError):
            asyncio.run(reader.command('get_states', entity_id='light.fixture'))

    def assess(self, data):
        return asyncio.run(inventory.assess(data,COMPONENT))

    def test_original_light_mapping_is_read_only_and_not_migration_approval(self):
        result=self.assess(snapshot())
        self.assertTrue(result['structure_passed'])
        self.assertEqual(result['legacy_bindings'][0]['pid'],'002')
        self.assertFalse(result['migration_allowed'])
        self.assertEqual(result['service_calls'],0)

    def test_duplicate_parent_and_missing_original_source_are_rejected(self):
        for key in ('entries','devices'):
            data=snapshot()
            if key=='entries':data[key].append(copy.deepcopy(data[key][0]))
            else:data[key].pop(0)
            with self.assertRaises(ValueError):self.assess(data)

    def test_disabled_entity_and_unavailable_state_do_not_rebind(self):
        for part,field,value in (('entities','disabled_by','user'),('states','state','unavailable')):
            data=snapshot();data[part][0][field]=value
            result=self.assess(data)
            self.assertFalse(result['structure_passed'])
            self.assertEqual(set(result['legacy_bindings'][0]['entities'].values()),{'light.fixture'})

    def test_order_ambiguous_mapping_is_a_blocker(self):
        data=snapshot();second={**data['entities'][0],'entity_id':'light.second','unique_id':'second'}
        data['entities'].append(second);data['states'].append({**data['states'][0],'entity_id':'light.second'})
        result=self.assess(data)
        self.assertIn('legacy_mapping_order_ambiguous',{r['code'] for r in result['issues']})

    def test_missing_HA_service_is_a_blocker(self):
        data=snapshot();data['services']['light'].remove('turn_on')
        self.assertIn('HA_service_missing',{r['code'] for r in self.assess(data)['issues']})

    def test_cross_device_roles_are_rejected_before_composition_read(self):
        data=snapshot()
        plan={'version':1,'devices':[{'id':'virtual-light-source','roles':{'main':'light.other'}}]}
        result=asyncio.run(inventory.assess(data,COMPONENT,plan,Path('unused')))
        self.assertFalse(result['structure_passed'])
        self.assertIn('entity_ownership',{r['code'] for r in result['issues']})

    def test_control_commands_are_rejected_before_network(self):
        reader=inventory.HAReader('http://test.invalid','not-a-secret')
        for command in ('call_service','config_entries/flow','config/entity_registry/update','supervisor/api','execute_script'):
            with self.assertRaises(ValueError):asyncio.run(reader.command(command))


class Owner:
    def stopped(self):return {'container_id':'virtual-stopped-owner','restart_disabled':True}
    def mac(self,_interface):return MAC


class ContainerGuardTest(unittest.TestCase):
    def setUp(self):
        self.config=Path(tempfile.gettempdir()).resolve()/'virtual-cold-guard-source'
        self.owner=cold.ContainerOwner('docker','virtual-ha',self.config)
        self.info={'Id':'virtual','State':{'Running':False,'Pid':0,'StartedAt':'old','FinishedAt':'stopped'},
            'HostConfig':{'RestartPolicy':{'Name':'no'},'NetworkMode':'host'},
            'Mounts':[{'Destination':'/config','Source':str(self.config),'RW':True}]}

    def test_stopped_exact_mount_passes(self):
        with patch.object(self.owner,'run',side_effect=[json.dumps([self.info]),'']):
            self.assertTrue(self.owner.stopped()['restart_disabled'])

    def test_running_restart_policy_network_and_wrong_mount_fail(self):
        for part,key,value in [('State','Running',True),('State','Pid',123),
            ('HostConfig','RestartPolicy',{'Name':'always'}),('HostConfig','NetworkMode','bridge')]:
            info=copy.deepcopy(self.info);info[part][key]=value
            with patch.object(self.owner,'run',return_value=json.dumps([info])),self.assertRaises(ValueError):self.owner.stopped()
        self.info['Mounts'][0]['Source']=str(self.config/'wrong')
        with patch.object(self.owner,'run',return_value=json.dumps([self.info])),self.assertRaises(ValueError):self.owner.stopped()

    def test_writable_alias_fails_and_readonly_reader_is_allowed(self):
        for writable in (True,False):
            alias={'Mounts':[{'Source':str(self.config.parent),'RW':writable}]}
            with patch.object(self.owner,'run',side_effect=[json.dumps([self.info]),'active',json.dumps([alias])]):
                if writable:
                    with self.assertRaises(ValueError):self.owner.stopped()
                else:self.assertTrue(self.owner.stopped()['restart_disabled'])

    def test_mutating_engine_commands_are_refused(self):
        for command in ('exec','start','stop','restart','rm','run','update'):
            with self.assertRaises(ValueError):self.owner.run(command,'virtual-ha')


class ColdExportTest(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.config=self.root/'config';self.config.mkdir()
        component=self.config/cold.COMPONENT
        shutil.copytree(COMPONENT/'config',component/'config')
        sdk=component/'hilink_bridge/lib/amd64/libhilink_bridge.so';sdk.parent.mkdir(parents=True)
        sdk.write_bytes(b'virtual-test-SDK-never-executed')
        codec=inventory.core_module('identity_store')
        self.addCleanup(patch.stopall)
        patch.object(codec,'SDK_SHA256',hashlib.sha256(sdk.read_bytes()).hexdigest()).start()
        patch.object(cold,'no_local_readers').start()  # Mac单测；真实Linux另测实际/proc和容器。
        data=snapshot();self.data=data
        entries={'data':{'entries':[{'entry_id':'original-entry','domain':'hwhomebridge','unique_id':'hwhomebridge','data':{},'options':{}},
                                    {'entry_id':'other','domain':'untouched','data':{'credential':'virtual-secret'}}]}}
        self.write(cold.REGISTRIES[0],entries)
        self.write(cold.REGISTRIES[1],{'data':{'devices':data['devices']}})
        self.write(cold.REGISTRIES[2],{'data':{'entities':data['entities']}})
        legacy=self.config/cold.RUNTIME/'config';legacy.mkdir(parents=True)
        conf,bridge=legacy_bytes(data['devices'][1]['identifiers'][0][2])
        for name,value in {'config.cfg':conf,'bridge.cfg':bridge,'device_ac':AC,'device_sn':b'EXISTING-GW-TEST\0',
                           'new_device.txt':b'virtual-light-source\n','excluded_devices.txt':b''}.items():
            (legacy/name).write_bytes(value)
        (legacy/'empty-private-directory').mkdir()
        self.report=asyncio.run(inventory.assess(data,component));self.report['two_pass_registry_stable']=True
        self.preflight=self.root/'preflight.json';self.preflight.write_text(json.dumps(self.report))
        self.out=self.root/'export'

    def write(self,name,value):
        path=self.config/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(value))

    def export(self,owner=None):
        return cold.export(self.config,self.out,owner or Owner(),'eth0',self.preflight)

    def test_complete_private_export_and_independent_verification(self):
        before={str(p.relative_to(self.config)):cold.read_file(p)[1] for p in self.config.rglob('*') if p.is_file()}
        result=self.export();self.assertTrue(result['passed']);self.assertTrue(cold.verify(self.out)['passed'])
        self.assertEqual(before,{str(p.relative_to(self.config)):cold.read_file(p)[1] for p in self.config.rglob('*') if p.is_file()})
        self.assertEqual((self.out/'payload'/cold.REGISTRIES[0]).read_bytes(),(self.config/cold.REGISTRIES[0]).read_bytes())
        self.assertTrue((self.out/'payload'/cold.RUNTIME/'config/empty-private-directory').is_dir())
        for path in (self.out,*self.out.rglob('*')):self.assertEqual(path.stat().st_mode & 0o077,0)

    def test_LF_serial_file_is_preserved_without_asserting_native_canonicalization(self):
        path = self.config/cold.RUNTIME/'config/device_sn'
        raw = b'EXISTING-GW-TEST\n';path.write_bytes(raw)
        self.assertTrue(self.export()['passed'])
        self.assertTrue(cold.verify(self.out)['passed'])
        self.assertEqual((self.out/'payload'/cold.RUNTIME/'config/device_sn').read_bytes(), raw)
        manifest = cold.read_json(self.out/'cold-export.json')
        self.assertIn('canonicalization pending', manifest['identity']['native_sn_storage_format'])
        self.assertFalse(manifest['adoption_ready'])

    def test_invalid_serial_encodings_still_refused(self):
        for raw in (b'NO-TERMINATOR', b'a\0b\n', b'a\nb\n', b'a\r\n', b'\n'):
            (self.config/cold.RUNTIME/'config/device_sn').write_bytes(raw)
            with self.assertRaises(ValueError): self.export()
            self.assertFalse(self.out.exists())

    def test_real_storage_owner_shape_is_accepted_but_class_changes_are_not(self):
        devices = copy.deepcopy(self.data['devices'])
        for row in devices: row['config_entry_id'] = row.pop('config_entries')[0]
        self.write(cold.REGISTRIES[1], {'data': {'devices': devices}})
        cold.validate_inventory(self.config, self.report)
        entities = copy.deepcopy(self.data['entities'])
        entities[0]['original_device_class'] = 'changed'
        self.write(cold.REGISTRIES[2], {'data': {'entities': entities}})
        with self.assertRaisesRegex(ValueError, 'registry changed'):
            cold.validate_inventory(self.config, self.report)

    def test_running_owner_never_creates_output(self):
        class Running(Owner):
            def stopped(self):raise ValueError('running')
        with self.assertRaises(ValueError):self.export(Running())
        self.assertFalse(self.out.exists())

    def test_wrong_mac_corruption_and_unmatched_child_are_rejected(self):
        class WrongMAC(Owner):
            def mac(self,_):return '02:11:22:33:44:99'
        with self.assertRaises(ValueError):self.export(WrongMAC())
        self.assertFalse(self.out.exists())
        path=self.config/cold.RUNTIME/'config/config.cfg';path.write_bytes(bytes(1588))
        with self.assertRaises(ValueError):self.export()
        self.assertFalse(self.out.exists())

    def test_backup_preserves_exclusions_and_reports_inventory_mismatch_without_adoption(self):
        path=self.config/cold.RUNTIME/'config/excluded_devices.txt';path.write_text('HBexcluded')
        self.report['legacy_inventory']=[];self.preflight.write_text(json.dumps(self.report))
        self.assertFalse(self.export()['adoption_ready'])
        result=cold.read_json(self.out/'cold-export.json')
        self.assertFalse(result['identity']['child_inventory_matches_live'])
        self.assertTrue(result['identity']['excluded_devices_present'])
        self.assertEqual((self.out/'payload'/cold.RUNTIME/'config/excluded_devices.txt').read_text(),'HBexcluded')

    def test_stale_registry_and_adapter_change_are_rejected(self):
        self.data['entities'][0]['unique_id']='changed'
        self.write(cold.REGISTRIES[2],{'data':{'entities':self.data['entities']}})
        with self.assertRaises(ValueError):self.export()
        self.write(cold.REGISTRIES[2],{'data':{'entities':snapshot()['entities']}})
        path=self.config/cold.COMPONENT/'config/adapters/default/002.json';path.write_bytes(path.read_bytes()+b' ')
        with self.assertRaises(ValueError):self.export()

    def test_owner_restart_during_capture_does_not_publish(self):
        class Restarted(Owner):
            count=0
            def stopped(self):
                self.count+=1
                return {'container_id':'virtual','started_at':self.count}
        with self.assertRaises(ValueError):self.export(Restarted())
        self.assertFalse(self.out.exists())

    def test_symlink_hardlink_and_destination_inside_source_are_rejected(self):
        link=self.config/cold.RUNTIME/'config/link';link.symlink_to(self.preflight)
        with self.assertRaises(ValueError):self.export()
        link.unlink()
        link.hardlink_to(self.config/cold.RUNTIME/'config/device_ac')
        with self.assertRaises(ValueError):self.export()
        link.unlink();self.out=self.config/'bad-export'
        with self.assertRaises(ValueError):self.export()

    def test_tampered_archive_and_extra_files_are_rejected(self):
        self.export();path=self.out/'payload'/cold.RUNTIME/'config/device_ac'
        path.write_bytes(bytes(48))
        with self.assertRaises(ValueError):cold.verify(self.out)

    def test_failed_entity_report_is_not_cold_evidence(self):
        self.report['inventory_complete']=False;self.preflight.write_text(json.dumps(self.report))
        with self.assertRaises(ValueError):self.export()

    def test_unavailable_entity_does_not_prevent_a_consistent_backup(self):
        self.report['structure_passed']=False;self.report['issues']=[{'code':'entity_unavailable'}]
        self.preflight.write_text(json.dumps(self.report))
        self.assertTrue(self.export()['passed'])
        result=cold.read_json(self.out/'cold-export.json')
        self.assertFalse(result['entity_preflight_passed'])
        self.assertFalse(result['adoption_ready'])

    def test_raw_restore_to_new_private_wrapper_preserves_bytes_and_metadata(self):
        self.export();target=self.root/'restored'
        with patch.object(cold.os,'geteuid',return_value=0):
            result=cold.restore(self.out,target)
        self.assertTrue(result['passed']);self.assertFalse(result['deployment_performed'])
        self.assertEqual(target.stat().st_mode & 0o777,0o700)
        manifest=cold.read_json(self.out/'cold-export.json')
        for name,info in manifest['files'].items():
            actual=(target/'config'/name).stat()
            self.assertEqual((actual.st_uid,actual.st_gid,actual.st_mode,actual.st_mtime_ns),
                             (info['uid'],info['gid'],info['mode'],info['mtime_ns']))
            self.assertEqual((target/'config'/name).read_bytes(),(self.config/name).read_bytes())
        self.assertTrue((target/'config'/cold.RUNTIME/'config/empty-private-directory').is_dir())

    def test_raw_restore_never_overwrites_original_export_or_existing_target(self):
        self.export();existing=self.root/'existing';existing.mkdir()
        for target in (self.config/'bad',self.out/'bad',existing):
            with patch.object(cold.os,'geteuid',return_value=0),self.assertRaises(ValueError):
                cold.restore(self.out,target)
        self.assertEqual(list(existing.iterdir()),[])

    def test_raw_restore_requires_admin_and_refuses_tampering(self):
        self.export();target=self.root/'restored'
        with patch.object(cold.os,'geteuid',return_value=1234),self.assertRaises(ValueError):
            cold.restore(self.out,target)
        (self.out/'payload'/cold.RUNTIME/'config/device_ac').write_bytes(bytes(48))
        with self.assertRaises(ValueError):cold.restore(self.out,target)
        self.assertFalse(target.exists())

    def test_raw_restore_refuses_special_modes_and_invalid_ownership(self):
        self.export();path=self.out/'cold-export.json';original=json.loads(path.read_text())
        for field,value in (('mode',0o104755),('uid',-1)):
            manifest=copy.deepcopy(original);next(iter(manifest['files'].values()))[field]=value
            path.write_text(json.dumps(manifest));path.chmod(0o600)
            with patch.object(cold.os,'geteuid',return_value=0),self.assertRaises(ValueError):
                cold.restore(self.out,self.root/'restored')
            self.assertFalse((self.root/'restored').exists())

    def test_raw_restore_failure_does_not_publish_partial_target(self):
        self.export();target=self.root/'restored';verified=cold.verify(self.out)
        with patch.object(cold.os,'geteuid',return_value=0),patch.object(cold,'verify',side_effect=[verified,ValueError('changed')]):
            with self.assertRaises(ValueError):cold.restore(self.out,target)
        self.assertFalse(target.exists())


if __name__=='__main__':unittest.main()
