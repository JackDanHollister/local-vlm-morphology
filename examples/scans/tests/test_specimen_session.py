"""Persistent transport and production worker tested without any Pi/serial/camera."""
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from specimen_lab import hardware
from specimen_lab.session import CaptureSession
from specimen_lab.store import Store


FAKE_SERVER = r'''
import json,sys,time,io
from pathlib import Path
from PIL import Image
source=json.loads(sys.stdin.buffer.readline())
ns={'__name__':'specimen_test_worker'}
exec(source.rsplit("\nemit({'state': 'bootstrapped'})",1)[0],ns)
emit=ns['emit'];read=ns['read_command']
emit({'state':'bootstrapped'})
init=read();position=dict(init['expected_position']);mode=sys.argv[2]
class Link:
 def disable(self):return ['OK DISABLE']
 def status(self):return [f'STATUS Y={position["Y"]:.3f} Z={position["Z"]:.3f}']
 def move(self,axis,steps,**kw):
  position.update(ns['check_target'](position,axis,steps,init['limits']))
  return ['OK ENABLE','OK SPEED','OK MOVE',*self.status(),'OK DISABLE']
class Request:
 def get_metadata(self):return {'SensorTimestamp':time.monotonic_ns()+20_000_000 if mode!='stale' else 0,'ExposureTime':20000}
 def save(self,name,path):Image.new('RGB',(3280,2464),'white').save(path,quality=95)
 def release(self):pass
class Camera:
 def capture_request(self,**kw):return Request()
worker=ns['Worker'](Path(sys.argv[1]),Link(),Camera(),init['limits'],position,init['max_steps'])
emit({'state':'ready','position':position})
while True:
 command=read()
 if command is None:break
 if command=={'op':'close'}:emit({'state':'closed','disable':['OK DISABLE']});break
 if mode=='disconnect':sys.exit(0)
 response=worker.action(command);emit(response)
 if response['report']['state']!='completed':break
'''


@pytest.fixture
def setup_session(tmp_path,monkeypatch):
    project=tmp_path/'project';(project/'scripts').mkdir(parents=True)
    (project/'scripts/openscan_motor_server.py').write_text('synthetic bridge version')
    monkeypatch.setattr(hardware,'PROJECT',project)
    store=Store(tmp_path/'store');store.specimen('test:session','Synthetic camera')
    guard=dict(limits={'Y':[0,2.5],'Z':[-1,2]},reference_position={'Y':0,'Z':0},
               recorded_at='2020-01-01T00:00:00+00:00',sha256='synthetic')
    original=subprocess.Popen;calls=[];mode=['ok']
    def popen(argv,**kwargs):
        calls.append(argv)
        return original([sys.executable,'-u','-c',FAKE_SERVER,str(tmp_path/'remote'),mode[0]],**kwargs)
    monkeypatch.setattr(subprocess,'Popen',popen)
    return store,guard,calls,mode


def capture(session,view='Y_PLUS_10_STEPS',photo=False):
    return session.capture(session.store,session.specimen_id,view,attended=True,session_id=session.session_id,
        max_travel_steps=2000,calibration=session.calibration,take_photo=photo,cancel=session.cancel,pose_extra={})


def test_one_connection_multiple_actions_fresh_frame_and_whole_session_lock(setup_session):
    store,guard,calls,_=setup_session
    with CaptureSession(store,'test:session','synthetic-session',guard,threading.Event()) as session:
        with pytest.raises(BlockingIOError):
            with hardware.lease():pass
        capture(session)
        image=capture(session,'Z_PLUS_10_STEPS',photo=True)
        assert image['pose']['firmware_position']=={'Y':.125,'Z':.125}
        assert (image['width'],image['height'])==(3280,2464)
    assert len(calls)==1 and store.audit()['ok']
    assert json.loads((session.folder/'terminal.json').read_text())['state']=='closed'
    with hardware.lease():pass
    actions=store.snapshot()['hardware_actions']
    assert len(actions)==2 and all(a['terminal']['state']=='completed' for a in actions)
    photo=next(a['terminal']['remote_report'] for a in actions if a['take_photo'])
    assert photo['exposure_started_monotonic_ns']>=photo['settled_monotonic_ns']


@pytest.mark.parametrize('mode',['stale','disconnect'])
def test_stale_frame_or_lost_reply_is_preserved_and_never_retried(setup_session,mode):
    store,guard,calls,modes=setup_session;modes[0]=mode
    with pytest.raises(ValueError):
        with CaptureSession(store,'test:session','failed-session',guard,threading.Event()) as session:
            capture(session,photo=True)
    actions=store.snapshot()['hardware_actions']
    assert len(actions)==1 and actions[0]['terminal']['state']=='uncertain'
    assert not store.snapshot()['images']
    with pytest.raises(ValueError,match='unresolved'):
        with CaptureSession(store,'test:session','must-not-retry',guard,threading.Event()):pass
    assert len(calls)==1


def test_stop_between_segments_prevents_next_send_and_closes_worker(setup_session):
    store,guard,calls,_=setup_session;stop=threading.Event()
    with CaptureSession(store,'test:session','stop-session',guard,stop) as session:
        capture(session)
        stop.set()
        with pytest.raises(ValueError,match='STOP'):
            capture(session)
    assert len(store.snapshot()['hardware_actions'])==1 and len(calls)==1
    assert json.loads((session.folder/'terminal.json').read_text())['state']=='closed'


def test_out_of_bounds_target_is_rejected_before_an_intent_or_command(setup_session):
    store,guard,calls,_=setup_session
    with CaptureSession(store,'test:session','bounds-session',guard,threading.Event()) as session:
        with pytest.raises(ValueError,match='crosses'):
            capture(session,'Y_MINUS_10_STEPS')
    assert not store.snapshot()['hardware_actions'] and len(calls)==1
