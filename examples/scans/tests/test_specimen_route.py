import copy
import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from specimen_lab import route,hardware,demo,providers,pipeline
from specimen_lab.motion import check_target
from specimen_lab.store import Store,write_new,canonical,now
from test_specimen_demo import result,finish


@pytest.fixture
def mapped(tmp_path,monkeypatch):
    source=Path(__file__).resolve().parents[1]/'config'
    project=tmp_path/'project';(project/'config').mkdir(parents=True)
    for name in ('scanner_calibration.json','specimen_40_photo_plan.json'):
        (project/'config'/name).write_bytes((source/name).read_bytes())
    monkeypatch.setattr(hardware,'PROJECT',project)
    r=route.load_if_available()
    class FakeSession:
        def __init__(self,*a):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def capture(self,*a,**kw):return hardware.capture(*a,**kw)
    monkeypatch.setattr(pipeline,'CaptureSession',FakeSession)
    store=Store(tmp_path/'store');store.specimen('test:route','Synthetic route fixture')
    return r,store


@pytest.mark.parametrize('change',['endpoint','transit','start','count','margin','budget'])
def test_route_rejects_unsafe_or_inconsistent_plan(mapped,change):
    r,_=mapped;c=copy.deepcopy(r['calibration']);p=copy.deepcopy(r['plan'])
    if change=='endpoint':p['photos'][-1]['target']['Z']=c['Z']['operator_viewing_range'][1]
    if change=='transit':p['photos'][0]['transit_commands'][0]['steps']=100
    if change=='start':p['start_position']['Y']+=.125
    if change=='count':p['photos'].pop()
    if change=='margin':c['Z']['demo_endpoint_margin_steps']=0
    if change=='budget':p['total_commanded_travel_steps']=1
    with pytest.raises(ValueError):route.validate(c,p)


def test_movement_only_action_keeps_reference_without_image_or_transfer(mapped,monkeypatch):
    r,s=mapped;calls=[]
    def run(argv,**kwargs):
        calls.append(argv)
        assert 'TAKE_PHOTO=False' in kwargs['input']
        intent=json.loads(next((s.root/'hardware').glob('*/*/intent.json')).read_text())
        before=r['guard']['reference_position'];after=check_target(before,'Z',10,r['guard']['limits'])
        report={'state':'completed','action_id':intent['action_id'],'position_after':after,
                'after':[f'STATUS Y={after["Y"]:.3f} Z={after["Z"]:.3f}']}
        return SimpleNamespace(returncode=0,stdout=json.dumps(report),stderr='')
    monkeypatch.setattr(hardware.subprocess,'run',run)
    output=hardware.capture(s,'test:route','Z_PLUS_10_STEPS',attended=True,session_id='mapped-test',
        calibration=r['guard'],take_photo=False,max_travel_steps=2000)
    assert output['firmware_position']['Z']==-9.5625 and len(calls)==1
    assert not s.snapshot()['images'] and s.snapshot()['hardware_actions'][0]['terminal']['state']=='completed'
    assert hardware.reference_position(s.snapshot()['hardware_actions'],r['guard'])==output['firmware_position']


def test_new_operator_anchor_cannot_hide_uncertain_action(mapped):
    r,_=mapped
    with pytest.raises(ValueError):hardware.reference_position([{'created':'2026-01-01T00:00:00+00:00','terminal':{'state':'uncertain'}}],r['guard'])


def fake_capture(store,r,calls,stop=None):
    def capture(s,specimen_id,view_id,**kwargs):
        before=hardware.reference_position(s.snapshot()['hardware_actions'],r['guard'])
        axis,steps=hardware.VIEWS[view_id];after=check_target(before,axis,steps,r['guard']['limits'])
        calls.append((axis,steps,kwargs['take_photo']))
        action_id='action_synthetic'+str(len(calls))
        folder=s.root/'hardware'/kwargs['session_id']/action_id
        write_new(folder/'intent.json',(canonical(dict(action_id=action_id,created=now(),steps=steps,axis=axis,
            session_id=kwargs['session_id'],view_id=view_id))+'\n').encode())
        report={'position_after':after,'after':[f'STATUS Y={after["Y"]:.3f} Z={after["Z"]:.3f}']}
        terminal={'state':'completed','remote_report':report}
        if kwargs['take_photo']:
            raw=io.BytesIO();Image.new('RGB',(50,40),'white').save(raw,'PNG')
            output=s.add_image(specimen_id,raw.getvalue(),pose={'firmware_position':after,**kwargs.get('pose_extra',{})})
            terminal['image_id']=output['id']
        else:output={'action_id':action_id,'firmware_position':after}
        write_new(folder/'terminal.json',(canonical(terminal)+'\n').encode())
        if stop:stop.set()
        return output
    return capture


def test_stop_between_tilt_transit_chunks_prevents_next_command(mapped,monkeypatch):
    r,s=mapped;calls=[];stop=threading.Event()
    # Photo 9 starts at photo 8's Y endpoint and needs a 240-step Z transit.
    anchor=copy.deepcopy(r['guard']);anchor['reference_position']=r['plan']['photos'][7]['target']
    r['guard']=anchor
    monkeypatch.setattr(hardware,'capture',fake_capture(s,r,calls,stop))
    with pytest.raises(ValueError,match='stopped'):route.capture_photo(s,'test:route',r,8,'test-transit',stop)
    assert calls==[('Z',50,False)] and not s.snapshot()['images']


def test_changed_start_rejected_before_any_transit(mapped,monkeypatch):
    r,s=mapped;r['guard']['reference_position']={'Y':3.0,'Z':-9.5625}
    monkeypatch.setattr(hardware,'capture',lambda *a,**kw:pytest.fail('Must not move'))
    with pytest.raises(ValueError,match='Recorded position'):route.capture_photo(s,'test:route',r,0,'test',threading.Event())


def test_complete_calibrated_job_captures_only_forty_photos_and_observes_all(mapped,monkeypatch):
    r,s=mapped;calls=[];observed=[]
    monkeypatch.setattr(hardware,'capture',fake_capture(s,r,calls))
    monkeypatch.setattr(providers,'provider',lambda p:dict(id=p,label=p,model='synthetic',available=True))
    def observe(store,specimen_id,images,provider_id,**kwargs):
        observed.append(list(images));run=store.begin_run(specimen_id,images,'synthetic',engine='synthetic-test')
        kwargs['on_started'](run['id'])
        return store.finish_run(run['id'],json.dumps(result(images[0])))
    monkeypatch.setattr(providers,'observe',observe)
    manager=demo.DemoManager(s);job=finish(manager,manager.start('test:route','codex',attended=True))
    assert job['state']=='complete' and job['views_captured']==job['views_completed']==40
    assert job['travel_steps']==1845 and len(calls)==68
    assert sum(photo for _,_,photo in calls)==40 and len(s.snapshot()['images'])==40
    assert [len(batch) for batch in observed]==[5]*8
    assert sorted(image for batch in observed for image in batch)==sorted(job['image_ids'])
    assert s.audit()['ok']


def test_explicit_continuation_reuses_five_photos_without_replaying_motion(mapped,monkeypatch):
    r,s=mapped;calls=[];observed=[]
    monkeypatch.setattr(hardware,'capture',fake_capture(s,r,calls))
    monkeypatch.setattr(providers,'provider',lambda p:dict(id=p,label=p,model='synthetic',available=True))
    def observe(store,specimen_id,images,provider_id,**kwargs):
        observed.append(list(images));run=store.begin_run(specimen_id,images,'synthetic',engine='synthetic-test')
        kwargs['on_started'](run['id'])
        return store.finish_run(run['id'],'{}' if len(observed)==1 else json.dumps(result(images[0])))
    monkeypatch.setattr(providers,'observe',observe)
    manager=demo.DemoManager(s)
    first=finish(manager,manager.start('test:route','codex',max_views=5,attended=True))
    terminal=manager.root/first['id']/'terminal.json';before=terminal.read_bytes()
    assert first['views_captured']==5 and first['views_completed']==0 and first['batches_failed']==1
    assert first['travel_steps']==110 and len(calls)==5
    second=finish(manager,manager.start('test:route','codex',attended=True,resume_job_id=first['id']))
    assert second['state']=='complete' and second['views_completed']==40
    assert second['reused_original_ids']==second['original_ids'][:5]==first['original_ids']
    assert second['travel_steps']==1845 and len(calls)==68
    assert sum(photo for _,_,photo in calls)==40 and len(s.snapshot()['images'])==40
    assert sorted(image for batch in observed[1:] for image in batch)==sorted(second['image_ids'])
    assert terminal.read_bytes()==before and s.audit()['ok']


def test_continuation_rejects_changed_counter_before_any_movement(mapped,monkeypatch):
    r,s=mapped;calls=[]
    monkeypatch.setattr(hardware,'capture',fake_capture(s,r,calls))
    monkeypatch.setattr(providers,'provider',lambda p:dict(id=p,label=p,model='synthetic',available=True))
    def observe(store,specimen_id,images,provider_id,**kwargs):
        run=store.begin_run(specimen_id,images,'synthetic',engine='synthetic-test')
        return store.finish_run(run['id'],json.dumps(result(images[0])))
    monkeypatch.setattr(providers,'observe',observe)
    manager=demo.DemoManager(s);first=finish(manager,manager.start('test:route','codex',max_views=5,attended=True))
    route.capture_photo(s,'test:route',r,5,'different-session',threading.Event())
    before=list(calls)
    with pytest.raises(ValueError,match='Position changed'):
        manager.start('test:route','codex',attended=True,resume_job_id=first['id'])
    assert calls==before


def test_reverse_route_starts_with_current_photo_and_preserves_all_targets(mapped):
    r,_=mapped
    reverse=route.from_position(r,r['plan']['photos'][-1]['target'])
    assert reverse['plan']['direction']=='reverse'
    assert reverse['plan']['photos'][0]['transit_commands']==[]
    assert reverse['plan']['total_commanded_travel_steps']==1835
    assert [p['target'] for p in reverse['plan']['photos']]==list(reversed([p['target'] for p in r['plan']['photos']]))
    assert route.verify_frozen(reverse,r)==reverse
    reverse['plan']['photos'][1]['transit_commands'][0]['steps']=-50
    with pytest.raises(ValueError):route.verify_frozen(reverse,r)


def test_new_scan_rejects_mid_route_position(mapped):
    r,_=mapped
    with pytest.raises(ValueError,match='endpoint'):
        route.from_position(r,r['plan']['photos'][10]['target'])


def test_capture_finishes_and_hardware_closes_before_parallel_model_calls(mapped,monkeypatch):
    import time
    r,s=mapped;calls=[];active=0;peak=0;lock=threading.Lock();closed=threading.Event()
    monkeypatch.setattr(hardware,'capture',fake_capture(s,r,calls))
    class Session:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):closed.set()
        def capture(self,*a,**kw):return hardware.capture(*a,**kw)
    monkeypatch.setattr(pipeline,'CaptureSession',Session)
    monkeypatch.setattr(providers,'provider',lambda p:dict(id=p,label=p,model='synthetic',available=True))
    def observe(store,specimen_id,images,provider_id,**kwargs):
        nonlocal active,peak
        assert closed.is_set() and sum(photo for _,_,photo in calls)==40
        with lock:active+=1;peak=max(peak,active)
        run=store.begin_run(specimen_id,images,'synthetic',engine='synthetic-test')
        kwargs['on_started'](run['id'])
        time.sleep(.1 if store.image(images[0])['pose']['route_photo']==1 else .03)
        output=store.finish_run(run['id'],json.dumps(result(images[0])))
        with lock:active-=1
        return output
    monkeypatch.setattr(providers,'observe',observe)
    manager=demo.DemoManager(s)
    job=finish(manager,manager.start('test:route','codex',attended=True,analysis_workers=2))
    assert job['state']=='complete' and job['views_completed']==40 and peak==2
    assert job['hardware_closed'] and not job['active_run_ids'] and job['batches_completed']==8
    assert [image for rid in job['run_ids'] for image in s.run(rid)['image_ids']]==job['image_ids']
    assert job['acquisition_finished']<=job['analysis_started']
    assert s.audit()['ok']


def test_stop_during_parallel_analysis_prevents_queued_model_calls(mapped,monkeypatch):
    r,s=mapped;calls=[];entered=threading.Event();release=threading.Event();observations=[]
    monkeypatch.setattr(hardware,'capture',fake_capture(s,r,calls))
    monkeypatch.setattr(providers,'provider',lambda p:dict(id=p,label=p,model='synthetic',available=True))
    def observe(store,specimen_id,images,provider_id,**kwargs):
        observations.append(images);entered.set();release.wait(2)
        run=store.begin_run(specimen_id,images,'synthetic',engine='synthetic-test')
        kwargs['on_started'](run['id'])
        return store.finish_run(run['id'],'',execution_error='Synthetic cancellation')
    monkeypatch.setattr(providers,'observe',observe)
    manager=demo.DemoManager(s);job=manager.start('test:route','codex',attended=True,analysis_workers=2)
    assert entered.wait(2) and sum(photo for _,_,photo in calls)==40
    manager.stop(job['id']);release.set();job=finish(manager,job)
    assert job['state']=='stopped' and len(observations)<=2
    assert all(b['state']=='cancelled' for b in job['batches'])
    assert s.audit()['ok']
