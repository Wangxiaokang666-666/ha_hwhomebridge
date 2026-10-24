"""真实 HA 配置流/实体注册表/进程生命周期验收；仅允许独立虚构清单。"""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from homeassistant import bootstrap, loader
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntryState, ConfigEntryDisabler, SOURCE_USER
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

CONFIG = Path('/config')
DIRECTORY = '.storage/hwhomebridge/gateways/current'
BASE = CONFIG / DIRECTORY
sys.path.insert(0, '/config')


def save(path, data):
    temporary = path.with_suffix('.tmp');temporary.write_text(json.dumps(data));temporary.chmod(0o600);temporary.replace(path)


async def make_hass():
    print('ACCEPTANCE bootstrap_begin',flush=True)
    assert json.loads((CONFIG/'fixture-scope.json').read_text()) == {
        'purpose':'existing-gateway-ha-lifecycle-virtual-only','production_access':False}
    hass = HomeAssistant('/config');loader.async_setup(hass);hass.config.skip_pip=True
    states = json.loads((CONFIG/'fixture-states.private.json').read_text())
    assert all(entity.split('.')[1].startswith('oh_single_') for entity in states)
    def sync():
        for entity,state in states.items():hass.states.async_set(entity,state['state'],state['attributes'])
        save(CONFIG/'fixture-states.private.json', states)
    hass.data['hwhomebridge_virtual_fixture'] = {'states': states, 'sync': sync}
    async def service(call):
        entity=call.data['entity_id'];assert entity in states
        state=states[entity]
        values={k:v for k,v in call.data.items() if k!='entity_id'}
        if call.service=='set_value':state['state']=str(values['value'])
        elif call.service=='select_option':state['state']=values['option']
        else:
            if call.service=='turn_on':state['state']='on'
            elif call.service=='turn_off':state['state']='off'
            elif call.service=='set_percentage':state['state']='on' if values['percentage'] else 'off'
            state['attributes'].update(values)
        # 虚构设备自行处理原生定时；这属于设备夹具，不是桥接内的软件倒计时。
        instance = json.loads((BASE / 'instances.json').read_text())
        for source in instance['devices']:
            roles = source['roles']
            for target in ('fan', 'light'):
                enable = roles[target + '_timer_enable']
                if entity == enable:
                    remaining = roles[target + '_timer_remaining']
                    duration = roles[target + '_timer_duration']
                    states[remaining]['state'] = states[duration]['state'] if state['state'] == 'on' else '0'
        with (CONFIG / 'fixture-service-calls.private.jsonl').open('a') as stream:
            stream.write(json.dumps({'time': time.time(), 'domain': call.domain, 'service': call.service,
                                     'entity': entity, 'values': values}) + '\n')
        sync()
    sync()
    result=await bootstrap.async_from_config_dict({'homeassistant':{'name':'Single existing gateway lifecycle fixture',
        'latitude':0,'longitude':0,'elevation':0,'unit_system':'metric','time_zone':'Asia/Shanghai'}},hass)
    assert result is hass
    print('ACCEPTANCE bootstrap_complete',flush=True)
    for domain in ('fan','light','switch','number','select'):
        assert await async_setup_component(hass,domain,{})
    from custom_components.hwhomebridge.ha_transport import ScopedTransport
    for domain,actions in {**ScopedTransport.ACTIONS,'select':{'select_option'}}.items():
        for action in actions:hass.services.async_register(domain,action,service)
    entries=hass.config_entries.async_entries('hwhomebridge');assert len(entries)==1
    entry=entries[0]
    bindings=json.loads((BASE/'bindings.json').read_text())['devices']
    instance=json.loads((BASE/'instances.json').read_text())
    devices={row['source_id']:set(row['entities'].values()) for row in bindings}
    for row in instance['devices']:devices.setdefault(row['id'],set()).update(row['roles'].values())
    device_reg=dr.async_get(hass);entity_reg=er.async_get(hass)
    for source,entities in devices.items():
        if device_reg.async_get(source) is None:
            # 固定历史来源ID的离线夹具；生产代码只调用公开注册表接口。
            device_reg._devices[source]=dr.DeviceEntry(config_entry_id=entry.entry_id,id=source,
                identifiers={('hwhomebridge_fixture',source)},name='Lifecycle virtual device')
        for entity in entities:
            domain=entity.split('.')[0]
            previous=entity_reg.async_get_entity_id(domain,'hwhomebridge_fixture',entity)
            if previous and previous!=entity:
                entity_reg.async_remove(previous)
            # 夹具预置状态没有平台所有者，先移除以免注册表自动追加 _2。
            hass.states.async_remove(entity)
            result=entity_reg.async_get_or_create(entity.split('.')[0],'hwhomebridge_fixture',entity,
                config_entry=entry,device_id=source,suggested_object_id=entity.split('.')[1])
            assert result.entity_id==entity,(entity,result.entity_id)
    device_reg.async_schedule_save()
    print('ACCEPTANCE fixture_registry_ready',flush=True)
    sync();await hass.async_start();await hass.async_block_till_done()
    print('ACCEPTANCE HA_started',flush=True)
    return hass,entry


async def options(hass,entry,mode):
    print('ACCEPTANCE select_'+mode,flush=True)
    flow=await hass.config_entries.options.async_init(entry.entry_id)
    flow=await hass.config_entries.options.async_configure(flow['flow_id'],{'action':'gateway_lifecycle'})
    assert flow['step_id']=='gateway_lifecycle'
    flow=await hass.config_entries.options.async_configure(flow['flow_id'],{'backend':mode,'gateway_directory':DIRECTORY})
    await hass.async_block_till_done()
    print('ACCEPTANCE selection_processed',flush=True)
    return flow


def runtime(hass,entry):return hass.data['hwhomebridge'][entry.entry_id]['runtime']


def snapshot():
    import hashlib
    paths=[BASE/'gateway.local.json',*(BASE/'runtime').glob('ha-child-*.json'),BASE/'legacy/config.cfg',BASE/'legacy/bridge.cfg']
    return {str(p.relative_to(BASE)):hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def child_processes():
    import psutil
    result=[]
    # HA OS 内核不一定提供 /proc/<pid>/task/<tid>/children。
    # 按父子关系遍历，仍检查真正的 SDK 所有者，并包含子线程派生的进程。
    for process in psutil.Process().children(recursive=True):
        try:cmd=' '.join(process.cmdline())
        except psutil.NoSuchProcess:continue
        if 'gateway_worker.py' in cmd or '/bridge_poc' in cmd:result.append(process.pid)
    return result


def check_native_identity_guard():
    root=json.loads((BASE/'gateway.local.json').read_text())
    candidate=BASE/'refused-gateway.private.json'
    try:
        for patch in ({'expected_cloud_id_sha256':'0'*64},
                      {'sn':'existing-formal-gateway','preserve_bound_identity':False}):
            save(candidate,{**root,**patch})
            result=subprocess.run([str(BASE/'bridge_poc'),str(BASE/'runtime'),'--live',str(candidate)],
                capture_output=True,text=True,timeout=20)
            assert result.returncode==2 and '"event":"sdk_start"' not in result.stdout
    finally:
        candidate.unlink(missing_ok=True)


async def post(hass,payload):
    import urllib.request
    def request():
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(urllib.request.Request('http://127.0.0.1:18093/bridge',data=json.dumps(payload).encode(),
            headers={'Content-Type':'application/json'}),timeout=15) as result:return json.load(result)
    return await hass.async_add_executor_job(request)


async def wait_active(hass,entry,mode):
    for _ in range(1500):
        current=hass.data.get('hwhomebridge',{}).get(entry.entry_id,{})
        value=current.get('runtime')
        if value and value.active==mode and current['backend']==mode:return value
        await asyncio.sleep(.1)
    raise TimeoutError('expected gateway backend did not become active')


async def exercise(phase):
    hass,entry=await make_hass();result={'phase':phase,'passed':False}
    identity=(entry.entry_id,entry.unique_id)
    expected=snapshot()
    try:
        if phase=='baseline':
            assert entry.state==ConfigEntryState.LOADED and entry.options['backend']=='disabled'
            assert not child_processes()
            save(CONFIG/'original-entry.private.json',{'entry_id':entry.entry_id,'unique_id':entry.unique_id})
            result.update(original_disabled_entry_loaded=True,existing_identity_files_retained=True)
        elif phase=='restored':
            assert entry.state==ConfigEntryState.LOADED and entry.options['backend']=='disabled'
            assert not child_processes()
            result['original_component_and_entry_restored']=True
        elif phase=='exercise':
            # 原SDK使用同一个三元标识；预置已有旧桥接条目，再核对接管复用。
            registry=dr.async_get(hass)
            old_ids={row['key']:registry.async_get_or_create(config_entry_id=entry.entry_id,
                identifiers={('hwhomebridge',entry.entry_id,row['sn'])},name='Existing virtual legacy device').id
                for row in json.loads((BASE/'bindings.json').read_text())['devices']}
            save(CONFIG/'original-bridge-device-ids.private.json',old_ids)
            duplicate=await hass.config_entries.flow.async_init('hwhomebridge',context={'source':SOURCE_USER})
            duplicate=await hass.config_entries.flow.async_configure(duplicate['flow_id'],{'backend':'existing_gateway'})
            assert duplicate['type']=='abort' and duplicate['reason']=='already_configured'
            result['adding_a_second_original_gateway_refused']=True
            await hass.async_add_executor_job(check_native_identity_guard)
            result['wrong_bound_identity_refused_before_SDK_start']=True
            # 误改映射只返回表单错误，原条目和身份均不变。
            binding=BASE/'bindings.json';original_binding=binding.read_bytes()
            try:
                binding.write_bytes(original_binding+b' ')
                refused=await options(hass,entry,'openharmony')
                assert refused['type']=='form' and refused['errors']['base']=='invalid_gateway_directory'
                assert not child_processes()
            finally:
                binding.write_bytes(original_binding)
            result['changed_mapping_refused_before_native_start']=True
            # 只注入驻留标记验证拒绝热切换；此HA进程确实没有加载旧库。
            hass.data['hwhomebridge']['legacy_attempted']=True
            try:
                assert (await options(hass,entry,'openharmony'))['type']=='create_entry'
                assert not child_processes() and runtime(hass,entry) is None
            finally:
                hass.data['hwhomebridge'].pop('legacy_attempted')
            result['resident_restart_guard_with_injected_marker']=True
            result['activate_legacy']= (await options(hass,entry,'legacy'))['type']=='create_entry'
            assert entry.state==ConfigEntryState.LOADED,(entry.state,entry.reason)
            old=runtime(hass,entry);assert old.active=='legacy' and len(child_processes())==1
            first=old.process
            assert (await options(hass,entry,'openharmony'))['type']=='create_entry'
            current=runtime(hass,entry);assert current.active=='openharmony' and first.returncode is not None
            assert len(child_processes())==1
            assert all(registry.async_get(device_id) is not None for device_id in old_ids.values())
            result['existing_legacy_bridge_registry_ids_preserved']=True
            result['switch_stops_old_owner_before_new']=True
            response=await post(hass,{'key':'legacy-light','sid':'switch','data':{'on':1}});assert response['ok']
            response=await post(hass,{'key':'fan-wifi','sid':'fan','data':{'speed':6}});assert response['ok'] and response['state']['fan']['speed']==6
            result['real_HA_service_write_readback']=True
            before_devices={d.id for d in dr.async_entries_for_config_entry(dr.async_get(hass),entry.entry_id)}
            stopped=current.process
            assert (await options(hass,entry,'disabled'))['type']=='create_entry'
            assert stopped.returncode is not None and not child_processes()
            assert snapshot()==expected
            result['disable_preserves_identity']=True
            await options(hass,entry,'openharmony');assert runtime(hass,entry).active=='openharmony'
            await hass.config_entries.async_reload(entry.entry_id);await hass.async_block_till_done()
            assert runtime(hass,entry).active=='openharmony' and len(child_processes())==1
            assert before_devices=={d.id for d in dr.async_entries_for_config_entry(dr.async_get(hass),entry.entry_id)}
            result['reload_preserves_HA_device_ids']=True
            stopped=runtime(hass,entry).process
            assert await hass.config_entries.async_set_disabled_by(entry.entry_id,ConfigEntryDisabler.USER)
            assert not child_processes() and stopped.returncode is not None and snapshot()==expected
            assert await hass.config_entries.async_set_disabled_by(entry.entry_id,None)
            await hass.async_block_till_done()
            assert runtime(hass,entry).active=='openharmony' and len(child_processes())==1
            result['HA_native_disable_enable_preserves_identity']=True
            await options(hass,entry,'legacy')
            async def kill_startup():
                for _ in range(9000):
                    for pid in child_processes():
                        try:cmd=(Path('/proc')/str(pid)/'cmdline').read_bytes()
                        except FileNotFoundError:continue
                        if b'/bridge_poc' in cmd:
                            os.kill(pid,signal.SIGKILL);return True
                    await asyncio.sleep(.02)
                raise TimeoutError('native candidate not observed')
            killer=asyncio.create_task(kill_startup())
            await options(hass,entry,'openharmony');assert await killer
            assert runtime(hass,entry).active=='legacy' and runtime(hass,entry).state['failed_backend']=='openharmony'
            result['killed_startup_candidate_rolls_back_same_identity']=True
            await options(hass,entry,'openharmony');assert runtime(hass,entry).active=='openharmony'
            current=runtime(hass,entry);current.process.kill()
            current=await wait_active(hass,entry,'legacy')
            assert current.state['failed_backend']=='openharmony' and len(child_processes())==1
            result['unexpected_native_exit_rolls_back']=True
            assert snapshot()==expected
        elif phase=='restart':
            current=await wait_active(hass,entry,'legacy')
            assert current.state['failed_backend']=='openharmony'
            result['failed_choice_stays_rolled_back_after_HA_restart']=True
            response=await post(hass,{'key':'legacy-light','sid':'switch','data':{'on':0}});assert response['ok']
            await options(hass,entry,'openharmony');assert runtime(hass,entry).active=='openharmony'
            result['explicit_retry_same_entry_succeeds']=True
        elif phase=='restart_openharmony':
            current=await wait_active(hass,entry,'openharmony')
            assert len(child_processes())==1 and snapshot()==expected
            response=await post(hass,{'key':'fan-wifi','sid':'fan','data':{'speed':3}});assert response['ok']
            result['native_selected_engine_restored_after_HA_restart']=True
        elif phase=='crash':
            current=await wait_active(hass,entry,'openharmony')
            original=json.loads((CONFIG/'original-entry.private.json').read_text())
            assert identity==(original['entry_id'],original['unique_id'])
            assert len(hass.config_entries.async_entries('hwhomebridge'))==1
            assert len(child_processes())==1 and snapshot()==expected
            response=await post(hass,{'key':'fan-wifi','sid':'fan','data':{'speed':3}});assert response['ok']
            assert not any('libhilink_bridge.so' in line for line in Path('/proc/self/maps').read_text().splitlines())
            save(CONFIG/'lifecycle-pre-crash-result.json',{'passed':True,
                'native_selected_engine_restored_after_HA_restart':True,'real_HA_fan_write_readback':True,
                'entry_id_and_unique_id_unchanged':True,'paired_files_unchanged':True,'one_config_entry':True,
                'legacy_library_not_loaded_into_HA_process':True})
            save(CONFIG/'lifecycle-crash-ready.private.json',{'ha_pid':os.getpid(),'worker_pid':current.process.pid})
            print('ACCEPTANCE hard_crash_ready',flush=True)
            await asyncio.Future()  # Linux 外部杀掉HA，验证内核清理所有者；不执行finally。
        original=json.loads((CONFIG/'original-entry.private.json').read_text())
        assert identity==(original['entry_id'],original['unique_id'])
        assert len(hass.config_entries.async_entries('hwhomebridge'))==1
        assert snapshot()==expected
        assert not any('libhilink_bridge.so' in line for line in Path('/proc/self/maps').read_text().splitlines())
        result.update(passed=True,entry_id_and_unique_id_unchanged=True,one_config_entry=True,paired_files_unchanged=True,
                      legacy_library_not_loaded_into_HA_process=True)
    finally:
        await hass.async_stop()
    assert not child_processes()
    result['all_owned_workers_stopped_with_HA']=True
    save(CONFIG/('lifecycle-'+phase+'-result.json'),result)
    print(json.dumps(result),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['baseline','exercise','restart','restart_openharmony','crash','restored']);a=p.parse_args()
    os.umask(0o077);logging.basicConfig(level=logging.WARNING)
    asyncio.run(exercise(a.phase))
