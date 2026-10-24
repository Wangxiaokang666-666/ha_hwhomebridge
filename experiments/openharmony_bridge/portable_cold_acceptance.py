"""仅独立Lima Linux：真实Docker + 官方HA OS用户空间的便携冷备验收。"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import shlex
import socket
import subprocess
import sys


def main(base, repo, runtime, haos, sdk, window_rehearsal=False):
    if (os.geteuid()!=0 or not socket.gethostname().startswith('lima-hwhomebridge-')
            or 'ID=haos' in Path('/etc/os-release').read_text() or base.exists()
            or base.parent != Path('/mnt/data') or not base.name.startswith('hwhomebridge-cold-runtime-test-')):
        raise ValueError('new synthetic fixture on the independent Lima host only')
    if 'VERSION_ID=18.3' not in (haos/'etc/os-release').read_text():
        raise ValueError('official HA OS 18.3 userspace required')
    base.mkdir(parents=True,mode=0o700)
    sys.path.insert(0,str(repo/'tools'))
    import ha_cold_export as cold
    import ha_entity_preflight as inventory
    import test_ha_cold_export as fixture
    config=base/'source';config.mkdir(mode=0o700)
    component=config/cold.COMPONENT;shutil.copytree(repo/cold.COMPONENT,component)
    target_sdk=component/'hilink_bridge/lib/amd64/libhilink_bridge.so';target_sdk.parent.mkdir(parents=True,exist_ok=True)
    assert hashlib.sha256(sdk.read_bytes()).hexdigest()==inventory.core_module('identity_store').SDK_SHA256
    shutil.copyfile(sdk,target_sdk)
    data=fixture.snapshot()
    def save(path,value):
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        path.write_text(json.dumps(value));path.chmod(0o600)
    entry={'entry_id':'original-entry','domain':'hwhomebridge','unique_id':'hwhomebridge','data':{},'options':{}}
    save(config/cold.REGISTRIES[0],{'data':{'entries':[entry,{'entry_id':'unrelated','domain':'fixture','data':{'credential':'synthetic-only'}}]}})
    save(config/cold.REGISTRIES[1],{'data':{'devices':data['devices']}})
    save(config/cold.REGISTRIES[2],{'data':{'entities':data['entities']}})
    fixture.MAC=cold.ContainerOwner('docker','virtual',config).mac('eth0')
    conf,bridge=fixture.legacy_bytes(data['devices'][1]['identifiers'][0][2])
    legacy=config/cold.RUNTIME/'config';legacy.mkdir(parents=True,mode=0o700)
    for name,value in {'device_sn':b'EXISTING-GW-TEST\0','device_ac':fixture.AC,'config.cfg':conf,'bridge.cfg':bridge,
                       'new_device.txt':b'virtual-light-source\n','excluded_devices.txt':b''}.items():
        (legacy/name).write_bytes(value);(legacy/name).chmod(0o640)
    (legacy/'empty').mkdir(mode=0o750)
    report=asyncio.run(inventory.assess(data,component));assert report['structure_passed']
    report['two_pass_registry_stable']=True;preflight=base/'preflight.private.json';save(preflight,report)
    before={n:cold.read_file(config/n)[1] for n in cold.source_files(config)}
    result={'passed':False,'synthetic_identity_only':True,'SDK_started':False,'official_HA_OS_userspace':'18.3',
            'kernel_and_PID1_from_independent_Ubuntu_host':True,'production_HA_OS_boot_tested':False}
    mounts=[];created=[]
    def run(*args,check=True,**kwargs):
        return subprocess.run(list(args),check=check,capture_output=True,text=True,timeout=180,**kwargs)
    def docker(*args,**kwargs):return run('docker','--host','unix:///var/run/docker.sock',*args,**kwargs)
    def bind(source,target):
        if source.is_dir():target.mkdir(parents=True,exist_ok=True)
        else:target.parent.mkdir(parents=True,exist_ok=True);target.touch()
        run('mount','--bind',str(source),str(target));mounts.append(target)
    def call(action,output,*,name='hwhomebridge-portable-source',target=None,ok=True,env=None,proof=None):
        args=['chroot',str(haos),'/opt/cold-runtime/cold-backup',action,'--output',str(output)]
        if action=='export':args+=['--engine','docker','--container',name,'--config',str(config),'--interface','eth0','--preflight',str(proof or preflight)]
        if target:args+=['--target',str(target)]
        completed=run(*args,check=False,env=env)
        save(base/(action+'-'+output.name+'.private.json'),{'returncode':completed.returncode,'stdout':completed.stdout,'stderr':completed.stderr})
        assert (completed.returncode==0)==ok, completed.stderr+completed.stdout
        value=json.loads(completed.stdout);assert value['passed']==ok
        if not ok and action=='export':assert not output.exists()
        return value
    def create(name,*options):
        assert docker('inspect',name,check=False).returncode==1
        docker('create','--name',name,'--network','host','--cap-drop','ALL','--read-only',
               '--mount',f'type=bind,src={config},dst=/config',*options,'--entrypoint','sleep',
               'hwhomebridge-maintenance-sleep:20261017','infinity');created.append(name)
    try:
        bind(Path('/proc'),haos/'proc');bind(Path('/sys'),haos/'sys');bind(Path('/dev'),haos/'dev')
        bind(Path('/run/docker.sock'),haos/'run/docker.sock')
        bind(base,haos/str(base).lstrip('/'));bind(runtime,haos/'opt/cold-runtime')
        assert run('chroot',str(haos),'sh','-c','command -v python3',check=False).returncode!=0
        health=run('chroot',str(haos),'/opt/cold-runtime/cold-backup','self-test')
        assert json.loads(health.stdout)['passed'];result['self_test_without_system_Python']=True
        result['runtime_versions']=json.loads(health.stdout)
        create('hwhomebridge-portable-source');docker('start','hwhomebridge-portable-source')
        call('export',base/'running-refused',ok=False);result['running_owner_refused']=True
        docker('stop','-t','0','hwhomebridge-portable-source')
        create('hwhomebridge-portable-restarting','--restart','always')
        call('export',base/'restart-refused',name='hwhomebridge-portable-restarting',ok=False);result['restart_policy_refused']=True
        create('hwhomebridge-portable-alias');docker('start','hwhomebridge-portable-alias')
        call('export',base/'alias-refused',ok=False);result['active_writable_alias_refused']=True
        docker('stop','-t','0','hwhomebridge-portable-alias')
        held=subprocess.Popen([sys.executable,'-S','-c','import sys;f=open(sys.argv[1]);print("ready",flush=True);input()',str(legacy/'config.cfg')],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
        try:
            assert held.stdout.readline().strip()=='ready'
            call('export',base/'fd-refused',ok=False);result['actual_host_file_owner_refused']=True
        finally:held.terminate();held.wait(timeout=10);held.stdin.close();held.stdout.close()
        poisoned={**os.environ,'PYTHONPATH':'/does/not/exist','PYTHONHOME':'/does/not/exist','PYTHONSTARTUP':'/does/not/exist'}
        exported=call('export',base/'export',env=poisoned);assert exported['paired_children']==1
        result['export_with_foreign_Python_environment_ignored']=True
        assert call('verify',base/'export')['passed'];result['offline_verify_passed']=True
        restored=base/'restored';assert call('restore',base/'export',target=restored)['passed']
        manifest=cold.read_json(base/'export/cold-export.json')
        for name,info in manifest['files'].items():
            path=restored/'config'/name;actual=path.stat()
            assert hashlib.sha256(path.read_bytes()).hexdigest()==info['sha256']
            assert (actual.st_uid,actual.st_gid,actual.st_mode,actual.st_mtime_ns)==(info['uid'],info['gid'],info['mode'],info['mtime_ns'])
        assert (restored/'config'/cold.RUNTIME/'config/empty').is_dir() and restored.stat().st_mode & 0o777==0o700
        result['new_private_directory_raw_restore_bytes_ownership_modes_mtime_passed']=True
        call('restore',base/'export',target=restored,ok=False);result['existing_restore_target_refused']=True
        corrupt=base/'export/payload'/cold.RUNTIME/'config/device_ac';raw=corrupt.read_bytes();corrupt.write_bytes(bytes(48))
        call('verify',base/'export',ok=False);call('restore',base/'export',target=base/'tampered-restore',ok=False)
        assert not (base/'tampered-restore').exists();corrupt.write_bytes(raw)
        result['tampered_export_verify_and_restore_refused']=True
        if window_rehearsal:
            # 仅测试容器生命周期；不模拟成 Supervisor/真实 HA 已验收。
            name='hwhomebridge-portable-source'
            original_id=json.loads(docker('inspect',name).stdout)[0]['Id']
            docker('start',name)
            resumed=json.loads(docker('inspect',name).stdout)[0]
            assert resumed['Id']==original_id and resumed['State']['Running']
            result['successful_backup_resumes_same_test_owner']=True
            # 已恢复运行时，可以继续在新目录离线还原；绝不覆盖源配置。
            assert call('restore',base/'export',target=base/'restore-after-resume')['passed']
            result['offline_restore_while_original_test_owner_runs']=True
            bad_proof=base/'stale-preflight.private.json'
            save(bad_proof,{**report,'registry_sha256':'stale-synthetic-proof'})
            docker('stop','-t','0',name)
            try:
                call('export',base/'stale-window-refused',proof=bad_proof,ok=False)
            finally:
                # 演练失败收尾。生产恢复需按运行手册核对源完整性后人工执行。
                docker('start',name)
            resumed=json.loads(docker('inspect',name).stdout)[0]
            assert resumed['Id']==original_id and resumed['State']['Running']
            result['failed_backup_resumes_same_test_owner_without_export']=True
            result['real_Supervisor_stop_start_tested']=False
            result['real_HA_or_SDK_started']=False
            docker('stop','-t','0',name)
        assert before=={n:cold.read_file(config/n)[1] for n in cold.source_files(config)}
        result.update(source_bytes_and_metadata_unchanged=True,passed=True)
    finally:
        for name in reversed(created):docker('rm','-f',name)
        for path in reversed(mounts):run('umount',str(path))
    result['owned_containers_removed_and_bind_mounts_unmounted']=True
    save(base/'core-result.private.json',result)
    namespace_refusal(base,runtime,haos)
    result['ordinary_container_self_test_and_Docker_inspect_pass_but_export_refused']=True
    save(base/'result.private.json',result);print(json.dumps(result))


def namespace_refusal(base,runtime,haos):
    """确认同一包能在容器加载，但不会把容器视图当成真实宿主导出。"""
    name='hwhomebridge-portable-namespace-owner'
    view='hwhomebridge-portable-namespace-view'
    prefix=['docker','--host','unix:///var/run/docker.sock']
    def docker(*args):return subprocess.run([*prefix,*args],check=True,capture_output=True,text=True,timeout=30)
    assert subprocess.run([*prefix,'inspect',name],capture_output=True).returncode==1
    assert subprocess.run(['podman','--remote=false','container','exists',view],capture_output=True).returncode==1
    docker('create','--name',name,'--network','host','--mount',f'type=bind,src={base}/source,dst=/config',
           '--entrypoint','sleep','hwhomebridge-maintenance-sleep:20261017','infinity')
    try:
        action=['/opt/cold-runtime/cold-backup','export','--output','/tmp/blocked','--engine','docker',
                '--container',name,'--config',str(base/'source'),'--interface','eth0','--preflight',str(base/'preflight.private.json')]
        command=('/opt/cold-runtime/cold-backup self-test && docker --host unix:///var/run/docker.sock inspect '+name+
                 ' >/dev/null && printf "docker-inspect-ok\\n" || exit 91; '+shlex.join(action)+
                 '; status=$?; test ! -e /tmp/blocked || exit 92; exit "$status"')
        args=['podman','--remote=false','run','--rm','--name',view,'--network','none','--read-only','--rootfs',
              '--mount',f'type=bind,src={runtime},dst=/opt/cold-runtime,ro=true',
              '--mount',f'type=bind,src={base},dst={base},ro=true',
              '--mount','type=bind,src=/run/docker.sock,dst=/run/docker.sock,ro=true',
              '--entrypoint','/usr/bin/sh',str(haos),'-c',command]
        done=subprocess.run(args,capture_output=True,text=True,timeout=180)
        assert done.returncode==1 and 'docker-inspect-ok' in done.stdout and '"passed": false' in done.stdout,done.stderr+done.stdout
    finally:
        subprocess.run(['podman','--remote=false','rm','-f',view],capture_output=True,timeout=30)
        docker('rm','-f',name)


if __name__=='__main__':
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('base','repo','runtime','haos','sdk'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--window-rehearsal',action='store_true')
    args=parser.parse_args();main(args.base,args.repo,args.runtime,args.haos,args.sdk,args.window_rehearsal)
