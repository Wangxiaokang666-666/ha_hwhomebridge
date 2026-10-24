"""独立Linux的真实Docker/Podman冷备导出验收；固定虚构身份，绝不启动SDK。"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def main(base, repo, sdk, image, engine='podman'):
    if base.exists():
        raise ValueError('new independent private directory required')
    base.mkdir(mode=0o700)
    sys.path.insert(0,str(repo/'tools'))
    import ha_cold_export as cold
    import ha_entity_preflight as inventory
    import test_ha_cold_export as fixture
    assert hashlib.sha256(sdk.read_bytes()).hexdigest()==inventory.core_module('identity_store').SDK_SHA256
    config=base/'source';config.mkdir(mode=0o700)
    component=config/cold.COMPONENT
    shutil.copytree(repo/cold.COMPONENT,component)
    target_sdk=component/'hilink_bridge/lib/amd64/libhilink_bridge.so'
    target_sdk.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(sdk,target_sdk)
    data=fixture.snapshot()
    entry={'entry_id':'original-entry','domain':'hwhomebridge','unique_id':'hwhomebridge','data':{},'options':{'backend':'disabled'}}
    def save(path,value):
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        path.write_text(json.dumps(value));path.chmod(0o600)
    save(config/cold.REGISTRIES[0],{'data':{'entries':[entry,{'entry_id':'unrelated','domain':'fixture','data':{'credential':'virtual-only'}}]}})
    save(config/cold.REGISTRIES[1],{'data':{'devices':data['devices']}})
    save(config/cold.REGISTRIES[2],{'data':{'entities':data['entities']}})
    owner=cold.ContainerOwner(engine,'hwhomebridge-cold-source',config)
    mac=owner.mac('eth0');fixture.MAC=mac
    conf,bridge=fixture.legacy_bytes(data['devices'][1]['identifiers'][0][2])
    legacy=config/cold.RUNTIME/'config';legacy.mkdir(parents=True,mode=0o700)
    for name,value in {'device_sn':b'EXISTING-GW-TEST\0','device_ac':fixture.AC,'config.cfg':conf,'bridge.cfg':bridge,
                       'new_device.txt':b'virtual-light-source\n','excluded_devices.txt':b''}.items():
        (legacy/name).write_bytes(value);(legacy/name).chmod(0o640)
    (legacy/'empty').mkdir(mode=0o750)
    first=asyncio.run(inventory.assess(data,component))
    second=asyncio.run(inventory.assess(json.loads(json.dumps(data)),component))
    assert first==second and first['structure_passed']
    first['two_pass_registry_stable']=True
    preflight=base/'preflight.private.json';save(preflight,first)
    source_before={n:cold.read_file(config/n)[1] for n in cold.source_files(config)}
    created=[];result={'passed':False,'engine':engine,'synthetic_credentials_only':True,'native_SDK_started':False}
    prefix=['podman','--remote=false'] if engine=='podman' else ['docker','--host','unix:///var/run/docker.sock']
    def container_command(*args):
        return subprocess.run([*prefix,*args],check=True,capture_output=True,text=True).stdout
    def create(name,*options):
        exists=['container','exists',name] if engine=='podman' else ['inspect',name]
        assert subprocess.run([*prefix,*exists],capture_output=True).returncode==1
        container_command('create','--name',name,'--network','host','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--read-only','--mount',f'type=bind,src={config},dst=/config',*options,'--entrypoint','sleep',image,'infinity')
        created.append(name)
    def refused(name, selected=owner):
        output=base/name
        try:cold.export(config,output,selected,'eth0',preflight)
        except ValueError:pass
        else:raise AssertionError('invalid source accepted')
        assert not output.exists()
        result[name]=True
    try:
        create(owner.name)
        container_command('start',owner.name)
        refused('actual_running_owner_refused')
        container_command('stop','-t','0',owner.name)
        create('hwhomebridge-cold-restarting','--restart','always')
        refused('actual_restart_policy_refused',cold.ContainerOwner(engine,'hwhomebridge-cold-restarting',config))
        create('hwhomebridge-cold-alias')
        container_command('start','hwhomebridge-cold-alias')
        refused('actual_writable_alias_refused')
        container_command('stop','-t','0','hwhomebridge-cold-alias')
        held=subprocess.Popen([sys.executable,'-S','-c',
            'import sys;f=open(sys.argv[1],"rb");print("ready",flush=True);input()',str(legacy/'config.cfg')],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            assert held.stdout.readline().strip()=='ready'
            refused('actual_local_file_owner_refused')
        finally:
            held.terminate();held.wait(timeout=10);held.stdin.close();held.stdout.close()
        exported=cold.export(config,base/'export',owner,'eth0',preflight)
        assert exported['passed'] and cold.verify(base/'export')['passed']
        result['actual_stopped_owner_export_and_verify']=True
        manifest=cold.read_json(base/'export/cold-export.json')
        assert manifest['identity']['root_id_sha256']==hashlib.sha256(b'11111111-2222-4333-8444-555555555555').hexdigest()
        assert manifest['all_HA_entries_included'] and manifest['runtime_KV_included']
        restored=base/'restored';shutil.copytree(base/'export/payload',restored)
        for name,info in manifest['files'].items():
            path=restored/name;path.chmod(info['mode'] & 0o7777)
            assert path.stat().st_uid==info['uid'] and path.stat().st_gid==info['gid']
            assert hashlib.sha256(path.read_bytes()).hexdigest()==info['sha256']
        for name,info in manifest['original_directories'].items():(restored/name).chmod(info['mode'] & 0o7777)
        assert (restored/cold.REGISTRIES[0]).read_bytes()==(config/cold.REGISTRIES[0]).read_bytes()
        assert (restored/cold.RUNTIME/'config/device_ac').read_bytes()==fixture.AC
        result['isolated_raw_restore_preserves_entries_identity_and_permissions']=True
        corrupt=base/'export/payload'/cold.RUNTIME/'config/device_ac';original=corrupt.read_bytes();corrupt.write_bytes(bytes(48))
        try:cold.verify(base/'export')
        except ValueError:result['tampered_export_refused']=True
        else:raise AssertionError('corrupt export accepted')
        corrupt.write_bytes(original)
        assert cold.verify(base/'export')['passed']
        assert source_before=={n:cold.read_file(config/n)[1] for n in cold.source_files(config)}
        assert not any('libhilink' in line for line in Path('/proc/self/maps').read_text().splitlines())
        result.update(source_files_and_metadata_unchanged=True,legacy_library_not_loaded=True,passed=True)
    finally:
        for name in reversed(created):container_command('rm','-f',name)
    result['all_owned_test_containers_removed']=True
    save(base/'result.json',result)
    print(json.dumps(result))


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('base',type=Path);parser.add_argument('--repo',required=True,type=Path);parser.add_argument('--sdk',required=True,type=Path)
    parser.add_argument('--image',default='ghcr.io/home-assistant/home-assistant:2026.10.1')
    parser.add_argument('--engine',choices=('docker','podman'),default='podman')
    args=parser.parse_args();main(args.base,args.repo,args.sdk,args.image,args.engine)
