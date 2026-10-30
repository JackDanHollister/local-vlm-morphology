import copy
import io
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from PIL import Image

from specimen_lab.contract import FEATURES, validate
from specimen_lab.hardware import check_request, remote_program, lease
from specimen_lab.observer import inspect_events
from specimen_lab.server import handler
from specimen_lab.store import Store
from specimen_lab.motion import VIEWS, positions, check_target, available_views

LIMITS={'Y':[0,2.5],'Z':[-.125,12.375]}


@pytest.fixture(autouse=True)
def isolate_hardware_config_and_lock(tmp_path, monkeypatch):
    from specimen_lab import hardware
    project=tmp_path/'hardware-fixture'
    (project/'config').mkdir(parents=True)
    (project/'config/machine_state.json').write_text(json.dumps({'axes':{
        axis:{'min_stop':bounds[0],'max_stop':bounds[1],'position':999}
        for axis,bounds in LIMITS.items()}}))
    monkeypatch.setattr(hardware,'PROJECT',project)


def test_saved_limits_and_step_units():
    pos=positions(['STATUS X=0.000 Y=0.450 Z=-0.063 A=0.000'])
    assert pos=={'Y':.45,'Z':-.0625}
    assert check_target(pos,'Z',10,LIMITS)=={'Y':.45,'Z':.0625}
    assert 'Z_MINUS_10_STEPS' not in available_views(pos,LIMITS)
    assert 'Z_PLUS_50_STEPS' in available_views(pos,LIMITS)
    assert 'Y_MINUS_50_STEPS' not in available_views(pos,LIMITS)
    assert {abs(steps) for axis,steps in VIEWS.values() if axis}=={10,25,50}


@pytest.mark.parametrize('axis,steps,pos', [('Z',-10,{'Y':.45,'Z':-.0625}),('Y',50,{'Y':2.0,'Z':0}),('Y',10,{'Y':-.0125,'Z':0}),('X',10,{'Y':.45,'Z':0}),('Y',5,{'Y':.45,'Z':0}),('Y',True,{'Y':.45,'Z':0})])
def test_reject_limits_and_non_catalogue_moves(axis,steps,pos):
    with pytest.raises(ValueError):check_target(pos,axis,steps,LIMITS)


@pytest.mark.parametrize('view', list(VIEWS))
def test_remote_program_uses_pre_move_limits_check(view):
    axis,steps=VIEWS[view]
    script=remote_program('action_abc123',axis,steps,LIMITS)
    compile(script,'remote','exec')
    assert script.index("report['target']=check_target")<script.index("report['move']=link.move")
    assert 'SET_LIMITS' not in script and 'OK ZERO' not in script


def test_position_reference_and_bad_scale():
    with pytest.raises(ValueError):positions(['STATUS Y=0.333 Z=0.000'])
    with pytest.raises(ValueError):positions(['STATUS Y=0.000'])
    script=remote_program('action_abc123','Y',10,LIMITS,{'Y':.45,'Z':-.0625})
    assert script.index('before!=EXPECTED_POSITION')<script.index("report['move']=link.move")


def test_proposals_use_live_record_not_saved_gui_position(store):
    from specimen_lab.hardware import movement_choices
    from specimen_lab.store import write_new, canonical
    folder=store.root/'hardware/test/action_test'
    write_new(folder/'intent.json',canonical({'action_id':'action_test','created':'2026-09-11','steps':350,'session_id':'test'}).encode())
    write_new(folder/'terminal.json',canonical({'state':'completed','remote_report':{'after':['STATUS Y=2.375 Z=-0.125']}}).encode())
    options=movement_choices(store,'test')
    assert options['position']=={'Y':2.375,'Z':-.125}
    assert 'Y_PLUS_10_STEPS' in options['views']
    assert 'Y_PLUS_25_STEPS' not in options['views']
    assert not any(v.startswith('Z_MINUS') for v in options['views'])
    assert options['used_steps']==350


def test_step_grid_targets_at_all_positions():
    for y_step in range(201):
        for delta in [-50,-25,-10,10,25,50]:
            if 0<=y_step+delta<=200:
                target=check_target({'Y':y_step/80,'Z':0},'Y',delta,LIMITS)
                assert target['Y']==(y_step+delta)/80


def payload(image_id):
    return dict(summary="Synthetic test only", next_view_id=None, next_view_reason="No more views", observations=[
        dict(feature_id=k,status="unknown",value=None,visibility="occluded",confidence=None,evidence=[],note="Occluded in test fixture") for k in FEATURES])


@pytest.fixture
def store(tmp_path):
    s=Store(tmp_path/'store');s.specimen('test:001','Synthetic fixture')
    out=io.BytesIO();Image.new('RGB',(100,80),'white').save(out,'PNG')
    s.add_image('test:001',out.getvalue())
    return s


def claim(p, key, image, value, visibility='clear', instances=()):
    obs=next(o for o in p['observations'] if o['feature_id']==key)
    obs.update(status='observed',value=value,visibility=visibility,evidence=[dict(image_id=image,bbox=[.1,.1,.3,.4],instance_id=i,description='Synthetic region') for i in (instances or [None])])
    return obs


def test_contract_unknown_and_distinct_counts():
    p=payload('im');assert validate(p,['im'])==[]
    o=claim(p,'legs.visible_count','im',2,'partial',['leg1','leg2'])
    assert validate(p,['im'])==[]
    o['evidence'].append(copy.deepcopy(o['evidence'][0]))
    assert validate(p,['im'])==[]  # same leg in multiple images is not summed
    o['value']=3;assert validate(p,['im'])
    o['value']=6;assert validate(p,['im'])


@pytest.mark.parametrize('case',['duplicate','missing','wrong_image','bad_box','unknown_value','negative_occluded','total_partial','zero_partial','wrong_type','not_a_vocabulary_colour'])
def test_reject_semantically_invalid(case):
    p=payload('im')
    if case=='duplicate':p['observations'].append(p['observations'][0])
    if case=='missing':p['observations'].pop()
    if case=='wrong_image':claim(p,'wings.present','other',True)
    if case=='bad_box':claim(p,'wings.present','im',True)['evidence'][0]['bbox']=[.9,.9,.2,.2]
    if case=='unknown_value':p['observations'][0]['value']=False
    if case=='negative_occluded':claim(p,'wings.present','im',False,'occluded')
    if case=='total_partial':claim(p,'legs.total_count','im',1,'partial',['leg1'])
    if case=='zero_partial':claim(p,'legs.visible_count','im',0,'partial')
    if case=='wrong_type':claim(p,'wings.present','im',1)
    if case=='not_a_vocabulary_colour':claim(p,'body.colours','im',['imaginary'])
    assert validate(p,['im'])


def test_quality_can_report_insufficient_detail():
    p=payload('im');claim(p,'image.fine_detail_sufficient','im',False,'insufficient_detail')
    assert not validate(p,['im'])


def test_image_integrity_crop_and_cross_specimen(store):
    i=store.snapshot()['images'][0]
    c=store.crop(i['id'],[10,20,30,40]);assert c['parent_id']==i['id'] and c['width']==30 and c['height']==40
    with pytest.raises(ValueError):store.crop(i['id'],[90,20,30,40])
    store.specimen('test:002','Other')
    with pytest.raises(ValueError):store.begin_run('test:002',[i['id']],'test')
    store.image_path(i['id']).write_bytes(b'changed')
    with pytest.raises(ValueError):store.begin_run('test:001',[i['id']],'test')


def test_raw_failure_and_immutable_terminal(store):
    i=store.snapshot()['images'][0]['id'];r=store.begin_run('test:001',[i],'synthetic')
    raw='{"broken": true}'
    final=store.finish_run(r['id'],raw);assert final['state']=='failed' and final['result'] is None
    assert (store.root/'runs'/r['id']/'raw-response.txt').read_text()==raw
    with pytest.raises(ValueError):store.finish_run(r['id'],json.dumps(payload(i)))


def test_reviews_and_graph_provenance(store):
    i=store.snapshot()['images'][0]['id'];r=store.begin_run('test:001',[i],'synthetic')
    p=payload(i);claim(p,'mount.pin_visible',i,True)
    assert store.finish_run(r['id'],json.dumps(p))['state']=='usable'
    store.review(r['id'],'mount.pin_visible','rejected','Synthetic test review',reviewer='automated_test')
    assert store.run(r['id'])['result']==p
    assert store.graph()==store.graph()
    nodes={n['id']:n for n in store.graph()['nodes']}
    assert all(e['source'] in nodes and e['target'] in nodes for e in store.graph()['edges'])
    ld=store.jsonld();records=[n.get('record',{}) for n in ld['@graph']]
    assert any(n.get('feature_id')=='mount.pin_visible' and n.get('evidence') for n in records)
    assert any(n.get('sha256') for n in records)
    assert store.audit()['ok']
    (store.root/'runs'/r['id']/'raw-response.txt').write_text('{}')
    assert not store.audit()['ok']


@pytest.mark.parametrize('view,used,attended,stopped', [('raw rm',0,True,False),('Y_PLUS_10_STEPS',395,True,False),('CURRENT',0,False,False),('CURRENT',0,True,True)])
def test_controller_rejects_unsafe_requests(view,used,attended,stopped):
    with pytest.raises(ValueError):check_request(view,used,attended,stopped)


def test_controller_bounded_program_and_exclusive_ownership():
    assert check_request('Y_PLUS_10_STEPS',390,True)==('Y',10)
    program=remote_program('action_abc123','Y',10,{'Y':[0,2.5],'Z':[-.125,12.375]});compile(program,'remote','exec')
    assert "link.move(MOVE_AXIS,MOVE_STEPS,speed=6000,disable=True)" in program
    assert 'ZERO' not in program and 'SET_LIMITS' not in program
    with lease():
        with pytest.raises(BlockingIOError):
            with lease(): pass


def test_observer_rejects_tool_calls_and_missing_completion():
    assert inspect_events([])[0]
    lines=[json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'ssh'}}),json.dumps({'type':'turn.completed','usage':{}})]
    assert inspect_events(lines)[0]
    assert not inspect_events([json.dumps({'type':'turn.completed','usage':{}})])[0]


def test_active_request_cannot_repeat_motion(store, monkeypatch):
    from specimen_lab import active
    i=store.snapshot()['images'][0]['id'];r=store.begin_run('test:001',[i],'synthetic',next_views=['Y_PLUS_10_STEPS'])
    p=payload(i);p['next_view_id']='Y_PLUS_10_STEPS';store.finish_run(r['id'],json.dumps(p))
    calls=[]
    def failed_capture(*args,**kwargs):
        calls.append(1);raise TimeoutError('Synthetic uncertain motor result')
    monkeypatch.setattr(active,'capture',failed_capture)
    with pytest.raises(ValueError):active.next_view(store,r['id'],attended=False,session_id='test')
    assert not calls
    with pytest.raises(TimeoutError):active.next_view(store,r['id'],attended=True,session_id='test')
    with pytest.raises(FileExistsError):active.next_view(store,r['id'],attended=True,session_id='test')
    assert len(calls)==1


def test_startup_diagnostic_is_narrowly_scoped():
    message='Code Mode is unavailable because code-mode host is disabled. Code mode will fail closed; enable `features.code_mode_host` and install `codex-code-mode-host`.'
    diagnostic=json.dumps({'type':'item.completed','item':{'type':'error','message':message}})
    start=json.dumps({'type':'turn.started'});end=json.dumps({'type':'turn.completed'})
    assert not inspect_events([diagnostic,start,end])[0]
    assert inspect_events([start,diagnostic,end])[0]


def test_hardware_timeout_blocks_following_action(store,monkeypatch):
    from specimen_lab import hardware
    import subprocess
    calls=[]
    def timeout(*args,**kwargs):
        calls.append(1);raise subprocess.TimeoutExpired('synthetic ssh',40)
    monkeypatch.setattr(hardware.subprocess,'run',timeout)
    with pytest.raises(subprocess.TimeoutExpired):hardware.capture(store,'test:001','Y_PLUS_10_STEPS',attended=True,session_id='synthetic')
    with pytest.raises(ValueError):hardware.capture(store,'test:001','Y_PLUS_10_STEPS',attended=True,session_id='synthetic')
    assert len(calls)==1


def test_presence_count_contradictions():
    p=payload('im');claim(p,'wings.present','im',False);claim(p,'wings.visible_count','im',1,'partial',['wing1'])
    assert validate(p,['im'])


def test_loopback_http_evidence_and_review_guards(store):
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(store));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base+'/api/snapshot') as r:assert json.load(r)['specimens'][0]['id']=='test:001'
        for path in ['/../index.sqlite3','/api/images/../../index.sqlite3','/api/capture']:
            with pytest.raises(HTTPError) as e:urlopen(base+path)
            assert e.value.code==404
        request=Request(base+'/api/reviews',b'{}',headers={'Content-Type':'application/json','Origin':'https://example.com'})
        with pytest.raises(HTTPError) as e:urlopen(request)
        assert e.value.code==403
        with pytest.raises(HTTPError) as e:urlopen(Request(base+'/',headers={'Host':'evil.example'}))
        assert e.value.code==403
        image=store.snapshot()['images'][0]['id'];run=store.begin_run('test:001',[image],'synthetic')
        store.finish_run(run['id'],json.dumps(payload(image)))
        request=Request(base+'/api/reviews',json.dumps({'run_id':run['id'],'feature_id':'wings.present','verdict':'needs_review','note':'Synthetic HTTP review test'}).encode(),headers={'Content-Type':'application/json','Origin':base})
        with urlopen(request) as r:assert r.status==201
        assert store.snapshot()['reviews'][0]['note']=='Synthetic HTTP review test'
    finally:
        server.shutdown();server.server_close();thread.join()
