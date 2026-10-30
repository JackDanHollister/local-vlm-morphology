import io
import json
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request,urlopen

from PIL import Image
import pytest

from specimen_lab import demo,providers
from specimen_lab.contract import FEATURES
from specimen_lab.server import handler
from specimen_lab.store import Store
from specimen_lab.process import communicate


def result(image_id,view=None):
    return dict(summary='Synthetic demo response',next_view_id=view,next_view_reason='Synthetic next view',
        observations=[dict(feature_id=k,status='unknown',value=None,visibility='occluded',confidence=None,evidence=[],note='Unknown is valid in the demonstration') for k in FEATURES])


@pytest.fixture
def setup_demo(tmp_path,monkeypatch):
    monkeypatch.setattr(demo.route,'load_if_available',lambda:None)
    store=Store(tmp_path/'store');store.specimen('test:demo','Synthetic demo fixture')
    raw=io.BytesIO();Image.new('RGB',(100,80),'white').save(raw,'PNG')
    for _ in range(3):store.add_image('test:demo',raw.getvalue())
    calls=[]
    def observe(s,specimen_id,image_ids,provider_id,**kw):
        calls.append(('observe',provider_id,len(image_ids)))
        run=s.begin_run(specimen_id,image_ids,'synthetic',next_views=kw.get('next_views',[]),engine='synthetic-test')
        if kw.get('on_started'):kw['on_started'](run['id'])
        return s.finish_run(run['id'],json.dumps(result(image_ids[0])))
    def capture(s,specimen_id,view_id,**kw):
        calls.append(('capture',view_id));assert kw['attended'] is True
        return s.add_image(specimen_id,raw.getvalue())
    monkeypatch.setattr(providers,'provider',lambda p:dict(id=p,label=p,model='synthetic',available=True))
    monkeypatch.setattr(providers,'observe',observe)
    monkeypatch.setattr(demo.hardware,'capture',capture)
    monkeypatch.setattr(demo.hardware,'movement_choices',lambda *a,**kw:{'views':[v for v in demo.hardware.VIEWS if v!='CURRENT']})
    return store,demo.DemoManager(store),calls


def finish(manager,job):
    deadline=time.monotonic()+4
    while time.monotonic()<deadline:
        current=manager.get(job['id'])
        if current['state'] in demo.TERMINAL:return current
        time.sleep(.01)
    raise AssertionError('Demo failed to reach terminal state')


def test_automatic_tour_captures_observes_and_saves_unknowns(setup_demo):
    store,manager,calls=setup_demo
    job=finish(manager,manager.start('test:demo','codex',max_views=3,attended=True))
    assert job['state']=='complete' and job['views_completed']==3
    assert [c for c in calls if c[0]=='capture']==[('capture','CURRENT'),('capture','Y_PLUS_50_STEPS'),('capture','Z_PLUS_50_STEPS')]
    assert [c[2] for c in calls if c[0]=='observe']==[3]
    assert len(store.snapshot()['runs'])==1 and not store.snapshot()['reviews']
    assert store.audit()['ok']


def test_recorded_mode_uses_selected_provider_without_hardware(setup_demo):
    _,manager,calls=setup_demo
    job=finish(manager,manager.start('test:demo','claude',source='recorded',max_views=3))
    assert job['state']=='complete' and job['views_completed']==3
    assert all(c[0]=='observe' and c[1]=='claude' for c in calls)


def test_adaptive_model_can_finish_with_unknown_features(setup_demo):
    _,manager,calls=setup_demo
    job=finish(manager,manager.start('test:demo','codex',mode='adaptive',attended=True))
    assert job['state']=='complete' and job['views_completed']==1
    assert [c for c in calls if c[0]=='capture']==[('capture','CURRENT')]


def test_stop_and_duplicate_start_during_observation(setup_demo,monkeypatch):
    _,manager,calls=setup_demo
    entered=threading.Event();release=threading.Event()
    original=providers.observe
    def blocked(*a,**kw):
        entered.set();release.wait(3);return original(*a,**kw)
    monkeypatch.setattr(providers,'observe',blocked)
    job=manager.start('test:demo','codex',attended=True)
    assert entered.wait(2)
    with pytest.raises(ValueError):manager.start('test:demo','codex',attended=True)
    manager.stop(job['id']);release.set()
    assert finish(manager,job)['state']=='stopped'
    assert len([c for c in calls if c[0]=='capture'])==5


def test_invalid_requests_never_reach_hardware(setup_demo):
    _,manager,calls=setup_demo
    for args in [{'attended':False},{'attended':True,'max_views':41},{'attended':True,'max_views':True},{'attended':True,'source':'remote'}]:
        with pytest.raises(ValueError):manager.start('test:demo','codex',**args)
    assert not calls


def test_store_stop_blocks_new_live_demo_but_allows_saved_images(setup_demo):
    store,manager,calls=setup_demo
    (store.root/'STOP').touch()
    with pytest.raises(ValueError,match='paused'):manager.start('test:demo','codex',attended=True)
    assert not calls and not manager.list()
    job=finish(manager,manager.start('test:demo','codex',source='recorded',max_views=3))
    assert job['state']=='complete' and all(c[0]=='observe' for c in calls)


def test_forty_photos_are_all_passed_in_eight_batches(setup_demo):
    store,manager,calls=setup_demo
    job=finish(manager,manager.start('test:demo','codex',attended=True))
    assert job['state']=='complete' and job['views_captured']==job['views_completed']==40
    assert len([c for c in calls if c[0]=='capture'])==40
    assert [c[2] for c in calls if c[0]=='observe']==[5]*8
    seen=[image for run in store.snapshot()['runs'] for image in run['image_ids']]
    assert seen==job['image_ids'] and len(set(seen))==40
    assert job['batches_completed']==8 and job['travel_steps']<=job['max_travel_steps']


def test_wide_tour_stays_inside_both_limits_without_repeating_poses():
    from specimen_lab.motion import available_views,check_target,VIEWS,DEMO_TRAVEL_STEPS
    limits={'Y':[0,2.5],'Z':[-.125,12.375]};position={'Y':1.825,'Z':.6875}
    poses=[position];used=0;directions={'Y':'PLUS','Z':'PLUS'};sizes=set()
    for i in range(1,40):
        choice=demo.tour_choice(available_views(position,limits,used,DEMO_TRAVEL_STEPS),i,directions)
        assert choice
        axis,steps=VIEWS[choice];position=check_target(position,axis,steps,limits)
        poses.append(position);used+=abs(steps);sizes.add(abs(steps))
    assert len({tuple(p.values()) for p in poses})==40 and sizes=={10,25,50}
    assert used<=DEMO_TRAVEL_STEPS
    from specimen_lab.hardware import check_request
    with pytest.raises(ValueError):check_request('Y_PLUS_10_STEPS',400,True)
    assert check_request('Y_PLUS_10_STEPS',400,True,max_travel_steps=DEMO_TRAVEL_STEPS)==('Y',10)


def test_capture_failure_is_terminal_without_retry(setup_demo,monkeypatch):
    _,manager,calls=setup_demo
    def failure(*a,**kw):calls.append(('failed_capture',));raise TimeoutError('synthetic failure')
    monkeypatch.setattr(demo.hardware,'capture',failure)
    job=finish(manager,manager.start('test:demo','codex',attended=True))
    assert job['state']=='failed' and calls==[('failed_capture',)]


def test_demo_http_start_guards_and_progress(setup_demo):
    store,manager,calls=setup_demo
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(store,manager))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_port}'
    body={'specimen_id':'test:demo','provider_id':'codex','source':'recorded','mode':'tour','max_views':2,'attended':False}
    try:
        with pytest.raises(HTTPError) as e:urlopen(Request(base+'/api/demos/start',json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':'https://other.example'}))
        assert e.value.code==403 and not calls
        with urlopen(Request(base+'/api/demos/start',json.dumps(body).encode(),headers={'Content-Type':'application/json','Origin':base})) as r:
            assert r.status==202;job=json.load(r)
        assert finish(manager,job)['views_completed']==2
        with urlopen(base+'/api/demos') as r:assert json.load(r)['jobs'][-1]['state']=='complete'
    finally:server.shutdown();server.server_close();thread.join()


def test_claude_transport_rejects_tools_and_failure():
    good=json.dumps({'type':'result','subtype':'success','is_error':False,'result':'{"summary":"synthetic"}'})
    assert not providers.parse_claude_events(good)[1]
    tool=json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash'}]}})
    assert providers.parse_claude_events(tool+'\n'+good)[1]
    assert providers.parse_claude_events(json.dumps({'type':'result','subtype':'error','is_error':True}))[1]


def test_local_provider_does_not_enable_without_configuration(monkeypatch):
    monkeypatch.delenv('SPECIMEN_LOCAL_VLM_URL',raising=False)
    monkeypatch.delenv('SPECIMEN_LOCAL_VLM_MODEL',raising=False)
    with pytest.raises(ValueError):providers.provider('local')


@pytest.mark.parametrize('bbox_format',['',providers.QWEN_BBOX_FORMAT])
@pytest.mark.parametrize('streaming',[False,True])
def test_local_adapter_sends_images_and_validates_returned_observation(tmp_path,monkeypatch,bbox_format,streaming):
    from http.server import BaseHTTPRequestHandler
    store=Store(tmp_path/'local');store.specimen('test:local','Synthetic local fixture')
    raw=io.BytesIO();Image.new('RGB',(40,30),'white').save(raw,'PNG')
    image=store.add_image('test:local',raw.getvalue());received=[]
    response_payload=result(image['id'])
    observed=next(o for o in response_payload['observations'] if o['feature_id']=='image.specimen_in_frame')
    observed.update(status='observed',value=True,visibility='clear',evidence=[{
        'image_id':image['id'],'bbox':[125,250,625,750] if bbox_format else [.125,.25,.5,.5],
        'instance_id':None,'description':'Synthetic specimen region'}])
    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            if streaming:
                content=json.dumps(response_payload)
                packets=[{'model':'synthetic-local','choices':[{'index':0,'delta':{'content':part},'finish_reason':None}]}
                         for part in (content[:len(content)//2],content[len(content)//2:])]
                packets.append({'model':'synthetic-local','choices':[{'index':0,'delta':{},'finish_reason':'stop'}]})
                packets.append({'choices':[],'usage':{'completion_tokens':123}})
                body=(''.join('data: '+json.dumps(p)+'\n\n' for p in packets)+'data: [DONE]\n\n').encode()
            else:
                body=json.dumps({'model':'synthetic-local','choices':[{'finish_reason':'stop','message':{'content':json.dumps(response_payload)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream' if streaming else 'application/json')
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Endpoint)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_URL',f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_MODEL','synthetic-local')
    monkeypatch.setenv('SPECIMEN_DEFAULT_PROVIDER','local')
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_BBOX_FORMAT',bbox_format)
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_TIMEOUT_SECONDS','1200')
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_STREAM','1' if streaming else '0')
    try:
        assert providers.catalog()[0]['id']=='local'
        run=providers.observe(store,'test:local',[image['id']],'local')
        assert run['state']=='usable'
        path,body=received[0]
        assert path=='/v1/chat/completions'
        assert body['messages'][0]['content'][1]['image_url']['url'].startswith('data:image/png;base64,')
        assert body['response_format']['json_schema']['schema']['required']
        assert body['temperature']==0 and body['seed']==0
        if streaming:
            assert body['stream'] is True and body['stream_options']['include_usage'] is True
            assert run['telemetry']['usage']['completion_tokens']==123
        folder=store.root/'runs'/run['id']
        assert json.loads((folder/'execution.json').read_text())['timeout_seconds']==1200
        assert json.loads((folder/'wire-schema.json').read_text())==body['response_format']['json_schema']['schema']
        region=next(o for o in run['result']['observations'] if o['feature_id']=='image.specimen_in_frame')
        assert region['evidence'][0]['bbox']==[.125,.25,.5,.5]
        if bbox_format:
            assert json.loads((folder/'wire-response.txt').read_text())==response_payload
            assert run['telemetry']['bbox_adapter']==providers.QWEN_BBOX_FORMAT
        assert store.audit()['ok']
    finally:server.shutdown();server.server_close();thread.join()


@pytest.mark.parametrize('cancelled',[False,True])
def test_local_stream_preserves_partial_output_on_failure(tmp_path,monkeypatch,cancelled):
    from http.server import BaseHTTPRequestHandler
    store=Store(tmp_path/'stream');store.specimen('test:stream','Synthetic stream fixture')
    raw_image=io.BytesIO();Image.new('RGB',(20,20),'white').save(raw_image,'PNG')
    image=store.add_image('test:stream',raw_image.getvalue())
    cancel=threading.Event()
    class Endpoint(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
            packet={'model':'synthetic-stream','choices':[{'index':0,'delta':{'content':'{"summary":'},'finish_reason':None}]}
            self.wfile.write(('data: '+json.dumps(packet)+'\n\n').encode());self.wfile.flush()
            if cancelled:
                time.sleep(.1);cancel.set()
                self.wfile.write(b': cancelled\n\n');self.wfile.flush()
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Endpoint)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_URL',f'http://127.0.0.1:{server.server_port}/v1')
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_MODEL','synthetic-stream')
    monkeypatch.setenv('SPECIMEN_LOCAL_VLM_STREAM','1')
    monkeypatch.delenv('SPECIMEN_LOCAL_VLM_BBOX_FORMAT',raising=False)
    try:
        run=providers.observe(store,'test:stream',[image['id']],'local',cancel=cancel)
        assert run['state']=='failed'
        assert any(('cancelled' if cancelled else 'terminal marker') in error for error in run['errors'])
        folder=store.root/'runs'/run['id']
        assert (folder/'raw-response.txt').read_text()=='{"summary":'
        assert 'synthetic-stream' in (folder/'http-response.sse').read_text()
        assert store.audit()['ok']
    finally:server.shutdown();server.server_close();thread.join()


def test_qwen_bbox_adapter_uses_native_wire_schema_and_normalizes():
    from specimen_lab.contract import output_schema
    schema=output_schema(['image_synthetic'])
    wire=providers.local_wire_schema(schema,providers.QWEN_BBOX_FORMAT)
    wire_bbox=wire['properties']['observations']['items']['anyOf'][0]['properties']['evidence']['items']['properties']['bbox']
    assert wire_bbox['items']=={'type':'integer','minimum':0,'maximum':1000}
    original_bbox=schema['properties']['observations']['items']['properties']['evidence']['items']['properties']['bbox']
    assert original_bbox['items']['maximum']==1
    payload={'observations':[{'evidence':[{'bbox':[125,250,625,750]}]}]}
    converted=providers.normalize_local_response(payload,providers.QWEN_BBOX_FORMAT)
    assert converted['observations'][0]['evidence'][0]['bbox']==[.125,.25,.5,.5]
    assert payload['observations'][0]['evidence'][0]['bbox']==[125,250,625,750]
    with pytest.raises(ValueError,match='x1 < x2'):
        providers.normalize_local_response(
            {'observations':[{'evidence':[{'bbox':[1,0,1,1]}]}]},providers.QWEN_BBOX_FORMAT)


def test_qwen_wire_schema_rejects_unsupported_negative_claims():
    from jsonschema import Draft202012Validator
    from specimen_lab.contract import output_schema
    validator=Draft202012Validator(providers.local_wire_schema(output_schema(['image_synthetic']),providers.QWEN_BBOX_FORMAT))
    payload=result('image_synthetic')
    assert validator.is_valid(payload)
    eye=next(o for o in payload['observations'] if o['feature_id']=='eyes.visible')
    eye.update(status='observed',value=False,visibility='occluded')
    assert not validator.is_valid(payload)
    eye['visibility']='clear'
    assert not validator.is_valid(payload)
    eye['evidence']=[dict(image_id='image_synthetic',bbox=[125,250,625,750],instance_id=None,description='Synthetic eye region')]
    assert validator.is_valid(payload)


def test_cancellation_terminates_observer_process():
    cancel=threading.Event()
    proc=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],stdin=subprocess.PIPE,start_new_session=True)
    timer=threading.Timer(.15,cancel.set);timer.start()
    started=time.monotonic()
    try:
        assert 'cancelled' in communicate(proc,b'fixture',20,cancel)
        assert proc.poll() is not None and time.monotonic()-started<4
    finally:
        timer.cancel()
        if proc.poll() is None:proc.kill();proc.wait()
        proc.stdin.close()


def test_fixed_tour_keeps_capturing_after_invalid_model_batch(setup_demo,monkeypatch):
    store,manager,calls=setup_demo;good=providers.observe;count=0
    def observe(s,specimen_id,image_ids,provider_id,**kw):
        nonlocal count
        count+=1
        if count!=1:return good(s,specimen_id,image_ids,provider_id,**kw)
        run=s.begin_run(specimen_id,image_ids,'synthetic',engine='synthetic-test')
        return s.finish_run(run['id'],'{}')
    monkeypatch.setattr(providers,'observe',observe)
    job=finish(manager,manager.start('test:demo','codex',attended=True))
    assert job['state']=='complete' and job['views_captured']==job['views_attempted']==40
    assert job['views_completed']==35 and job['batches_failed']==1 and job['batches_completed']==7
    assert len([c for c in calls if c[0]=='capture'])==40
    assert store.run(job['run_ids'][0])['state']=='failed'
    assert store.audit()['ok']
