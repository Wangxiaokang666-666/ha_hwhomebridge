"""真实HA进程重启、强杀及恢复验收；只接受虚拟实验目录与原有测试容器。"""
import argparse
import json
from pathlib import Path
import subprocess
import time


def main(base, container, after_exercise):
    config = base / 'config'
    assert json.loads((config/'fixture-scope.json').read_text()) == {
        'purpose':'existing-gateway-ha-lifecycle-virtual-only','production_access':False}
    root = json.loads((config/'.storage/hwhomebridge/gateways/current/gateway.local.json').read_text())
    assert root['sn'].startswith('OHPOC-')
    info = json.loads(subprocess.check_output(['podman','inspect',container]))[0]
    assert info['State']['Running'] and any(m['Destination']=='/config' and Path(m['Source'])==config for m in info['Mounts'])
    def command(*args):
        return ['podman','exec',container,*args]
    def read_result(phase):
        result = json.loads((config/('lifecycle-'+phase+'-result.json')).read_text())
        assert result['passed']
        return result
    def run(phase):
        with (base/(phase+'-sequence.private.log')).open('w') as log:
            subprocess.run(command('python3','/tests/gateway_ha_acceptance.py',phase),stdout=log,stderr=subprocess.STDOUT,check=True)
        result = read_result(phase)
        print('completed_real_HA_'+phase,flush=True)
        return result
    if after_exercise:
        for _ in range(1800):
            if (config/'lifecycle-exercise-result.json').exists():
                read_result('exercise');time.sleep(2);break
            time.sleep(1)
        else:
            raise TimeoutError('initial real-HA lifecycle exercise did not pass')
    else:
        run('exercise')
    results = {'restart_after_fallback':run('restart')}
    ready = config/'lifecycle-crash-ready.private.json'
    ready.unlink(missing_ok=True)
    with (base/'crash-sequence.private.log').open('w') as log:
        crash = subprocess.Popen(command('python3','/tests/gateway_ha_acceptance.py','crash'),stdout=log,stderr=subprocess.STDOUT)
        try:
            for _ in range(900):
                if ready.exists():break
                assert crash.poll() is None,'HA exited before crash readiness'
                time.sleep(1)
            else:
                raise TimeoutError('HA did not reach stable native state')
            identity = json.loads(ready.read_text())
            results['normal_native_restart'] = read_result('pre-crash')
            ha_pid, worker_pid = int(identity['ha_pid']),int(identity['worker_pid'])
            check = ('from pathlib import Path; '
                f'h=Path("/proc/{ha_pid}/cmdline").read_bytes();w=Path("/proc/{worker_pid}/cmdline").read_bytes();'
                'assert b"gateway_ha_acceptance.py" in h and b"crash" in h and b"/bridge_poc" in w')
            subprocess.run(command('python3','-c',check),check=True,capture_output=True)
            subprocess.run(command('kill','-9',str(ha_pid)),check=True,capture_output=True)
            assert crash.wait(timeout=30)==137
            check = ('from pathlib import Path; '
                f'p=Path("/proc/{worker_pid}/status");'
                'assert not p.exists() or any(x.startswith("State:") and "Z" in x for x in p.read_text().splitlines())')
            subprocess.run(command('python3','-c',check),check=True,capture_output=True)
            results['hard_crash']={'passed':True,'actual_HA_process_SIGKILL':True,'native_owner_not_running':True,
                                   'shutdown_hook_not_executed':True}
            print('completed_HA_SIGKILL_and_kernel_owner_cleanup',flush=True)
        finally:
            if crash.poll() is None:
                # 仅停止前面核对过的虚拟测试容器，确保失败路径也没有活跃所有者。
                subprocess.run(['podman','stop','-t','0',container],check=True,capture_output=True)
                crash.wait(timeout=30)
    results['restart_after_SIGKILL'] = run('restart_openharmony')
    output = base/'restart-sequence-result.json'
    output.write_text(json.dumps({'passed':True,**results},indent=2)+'\n');output.chmod(0o600)
    print('all_real_HA_restart_and_crash_checks_passed',flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('base',type=Path)
    parser.add_argument('--container',default='hwhomebridge-ha-lifecycle-runner')
    parser.add_argument('--after-exercise',action='store_true')
    args=parser.parse_args()
    main(args.base,args.container,args.after_exercise)
