"""虚构Docker宿主证据、SSH参数隔离及旧日志证据分级；不访问生产。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import ha_host_preflight as host
import ha_legacy_binding_audit as binding


def evidence():
    rows = {'version':'1','complete':'true','uid':'0','init':'systemd','container_context':'false',
            'docker_info':json.dumps({'id':'fixture-daemon','root':'/var/lib/docker','security':[]}),
            'owner':json.dumps({'id':'fixture-core','state':{'Running':True,'Pid':10,'StartedAt':'fixture'},
                                'network':'host','restart':{'Name':'always'},'user':''}),
            'mounts':json.dumps([{'Source':'/fixture/config','Destination':'/config','RW':True}]),
            'config':'/fixture/config','runtime_uid':'0','host_net':'net:[1]','core_net':'net:[1]',
            'interface_mac':'02:11:22:33:44:55','python':''}
    for name in host.STAT_PATHS:
        rows['stat:'+name]=json.dumps({'uid':0,'gid':0,'mode':'700','size':4,
                                     'type':'directory' if name in host.STAT_PATHS[:3] else 'regular file'})
    for protocol in ('tcp','tcp6','udp','udp6'):rows['ports:'+protocol]='00000000:0050 '
    return rows


def encode(rows):
    return '\n'.join(key+'\t'+value for key,value in rows.items()).encode()


class HostTest(unittest.TestCase):
    def test_running_host_inspection_is_not_cold_backup_or_permission_to_install(self):
        result=host.assess(encode(evidence()))
        self.assertTrue(result['actual_Docker_inspected'])
        self.assertTrue(result['host_channel_verified'])
        for key in ('cold_backup','production_install_allowed','Core_stopped_tested','cold_export_dependencies_verified','host_python_found'):
            self.assertFalse(result[key])
        self.assertEqual(result['observed_ports']['tcp'],[80])

    def test_container_namespace_wrong_user_and_incomplete_probe_rejected(self):
        for key,value in (('container_context','true'),('uid','1000'),('init','bash'),('complete','false'),('core_net','net:[2]')):
            rows=evidence();rows[key]=value
            with self.assertRaises(ValueError):host.assess(encode(rows))

    def test_remote_daemon_rootless_or_inexact_mount_rejected(self):
        for key,value in (('docker_info',{'id':'rootless','root':'/rootless','security':['name=rootless']}),
                          ('mounts',[{'Source':'/other','Destination':'/config','RW':True}])):
            rows=evidence();rows[key]=json.dumps(value)
            with self.assertRaises(ValueError):host.assess(encode(rows))

    def test_duplicate_and_oversized_output_rejected(self):
        with self.assertRaises(ValueError):host.assess(encode(evidence())+b'\nuid\t0')
        with patch.object(host,'MAX_OUTPUT',1):
            with self.assertRaises(ValueError):host.assess(b'ab')

    def test_symlink_or_bad_mode_rejected(self):
        for field,value in (('type','symbolic link'),('mode','888')):
            rows=evidence();item=json.loads(rows['stat:.storage/core.config_entries']);item[field]=value
            rows['stat:.storage/core.config_entries']=json.dumps(item)
            with self.assertRaises(ValueError):host.assess(encode(rows))

    def test_stopped_core_not_accepted_as_online_preflight(self):
        rows=evidence();owner=json.loads(rows['owner']);owner['state']['Running']=False
        rows['owner']=json.dumps(owner)
        with self.assertRaises(ValueError):host.assess(encode(rows))

    def test_ssh_has_no_forwarding_or_shell_injection(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'private';path.write_text('virtual');path.chmod(0o600)
            argv=host.ssh_arguments('ha.test',22222,path,path)
            self.assertIn('StrictHostKeyChecking=yes',argv)
            self.assertIn('IdentityAgent=none',argv)
            for name in ('-oProxyCommand=bad','host;bad','root@ha.test'):
                with self.assertRaises(ValueError):host.ssh_arguments(name,22222,path,path)
            path.chmod(0o644)
            with self.assertRaises(ValueError):host.ssh_arguments('ha.test',22222,path,path)

    def test_failed_ssh_does_not_publish_report(self):
        with tempfile.TemporaryDirectory() as folder:
            output=Path(folder).resolve()/'new'
            with patch.object(host,'ssh_arguments',return_value=['ssh']), patch.object(host.subprocess,'run') as run:
                run.return_value.returncode=255
                with self.assertRaises(ValueError):host.collect('ha.test',22222,Path('key'),Path('known'),output)
            self.assertFalse(output.exists())
            self.assertEqual(run.call_args.args[0][-1],'sh -s -- homeassistant enp2s1')


class BindingTest(unittest.TestCase):
    def report(self,raw):
        data={'legacy_bindings':[{'source_id':'virtual-source','sn':'virtual-sn','entities':{'switch':'light.main'},
                                  'ambiguous_choices':{'switch':['light.main','light.ambient']}}]}
        return binding.audit(raw,data,'virtual-source')

    def test_no_logs_preserves_ambiguity_without_confirming_actual_binding(self):
        result=self.report(b'ordinary unrelated log')
        self.assertFalse(result['historical_routes_observed'])
        self.assertFalse(result['original_runtime_bindings_confirmed'])
        self.assertIn('switch',result['ambiguous_choices'])

    def test_historical_explicit_route_is_not_current_runtime_proof(self):
        raw=b'2026-01-01 INFO [custom_components.hwhomebridge.service_router] route_action: sn=virtual-sn, service=switch, entity=light.main, payload={}'
        result=self.report(raw)
        self.assertTrue(result['historical_routes_observed'])
        self.assertEqual(result['historical_routes'][0]['entity'],'light.main')
        self.assertFalse(result['original_runtime_bindings_confirmed'])
        self.assertFalse(result['migration_allowed'])

    def test_other_logger_and_other_device_not_attributed_to_target(self):
        raw=b'[other] route_action: sn=virtual-sn, service=switch, entity=light.main, payload={}\n[custom_components.hwhomebridge.service_router] route_action: sn=other-sn, service=switch, entity=light.main, payload={}'
        self.assertFalse(self.report(raw)['historical_routes_observed'])

    def test_log_reader_fixed_GET_and_no_redirect(self):
        class Response:
            def __enter__(self):return self
            def __exit__(self,*_args):pass
            def read(self,_limit):return b'virtual log'
        with patch.object(binding.urllib.request,'build_opener') as opener:
            opener.return_value.open.return_value=Response()
            self.assertEqual(binding.read_logs('http://ha.test','fake-token'),b'virtual log')
            request=opener.return_value.open.call_args.args[0]
            self.assertEqual(request.full_url,'http://ha.test/api/hassio/core/logs/latest')
            self.assertEqual(request.get_method(),'GET')
            self.assertIsNone(request.data)


if __name__=='__main__':unittest.main()
