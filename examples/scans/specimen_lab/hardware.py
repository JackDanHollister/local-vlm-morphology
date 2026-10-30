"""Attended, bounded SSH capture broker. No model-provided shell or axis commands.

Current firmware cannot interrupt a MOVE; a stop file prevents subsequent moves.
Every command has a fresh unique action directory and is never automatically retried.
"""

from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
from pathlib import Path
import subprocess

from .store import now, uid, write_new, canonical
from .motion import VIEWS, MAX_TRAVEL_STEPS, POLICY_VERSION, validate_limits, positions, available_views, travel_budget, check_target

PROJECT = Path(__file__).resolve().parents[1]
SSH = ["ssh", "-i", str(Path.home()/".ssh/openscan_key"), "-o", "IdentitiesOnly=yes",
       "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=5",
       "scanner@scanner.invalid"]

def saved_limits():
    raw = (PROJECT / "config/machine_state.json").read_bytes()
    state = json.loads(raw)
    limits = {axis: [state["axes"][axis]["min_stop"], state["axes"][axis]["max_stop"]] for axis in ("Y", "Z")}
    return validate_limits(limits), hashlib.sha256(raw).hexdigest()


def movement_choices(store, session_id, max_travel_steps=MAX_TRAVEL_STEPS):
    """Proposal menu from recorded state; live checks still precede transmission."""
    actions=store.snapshot()["hardware_actions"]
    if any(a["terminal"]["state"]!="completed" for a in actions):
        raise ValueError("Resolve outstanding hardware action before offering movements")
    recorded=[a for a in actions if a["terminal"].get("remote_report",{}).get("after")]
    if not recorded:
        raise ValueError("No recorded firmware position; perform an attended qualification first")
    last=max(recorded,key=lambda a:a["created"])
    position=positions(last["terminal"]["remote_report"]["after"])
    limits,sha=saved_limits()
    used=sum(abs(a['steps']) for a in actions if a['session_id']==session_id)
    return dict(views=available_views(position,limits,used,max_travel_steps),position=position,saved_limits=limits,
                reference_action=last['action_id'],used_steps=used,max_travel_steps=max_travel_steps,
                policy_version=POLICY_VERSION,machine_state_sha256=sha,
                note="Last recorded position; controller rechecks live state before each move")


def check_request(view_id, used_steps, attended, stopped=False, max_travel_steps=MAX_TRAVEL_STEPS):
    travel_budget(max_travel_steps)
    if not attended:
        raise ValueError("Live capture requires the attended option")
    if stopped:
        raise ValueError("STOP file is present; no new capture or move")
    if view_id not in VIEWS:
        raise ValueError("View is not in the fixed controller catalogue")
    axis, steps = VIEWS[view_id]
    if type(used_steps) is not int or used_steps < 0 or used_steps + abs(steps) > max_travel_steps:
        raise ValueError("Session movement budget exceeded")
    return axis, steps


@contextmanager
def lease():
    path = PROJECT / "logs/scanner-hardware.lock"
    path.parent.mkdir(exist_ok=True)
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def remote_program(action_id, axis, steps, limits, expected_position=None, take_photo=True):
    if not action_id.startswith("action_") or not action_id[7:].isalnum() or (axis, steps) not in VIEWS.values() or type(steps) is not int:
        raise ValueError("Invalid remote action")
    validate_limits(limits)
    if type(take_photo) is not bool:raise ValueError('Invalid capture option')
    # Fixed code with validated generated ID and integer; no model/user shell text.
    program = '''import fcntl, hashlib, json, os, pathlib, re, subprocess, sys, time
root=pathlib.Path('/opt/openscan-mini')
assert pathlib.Path('/sys/class/net/eth0/address').read_text().strip()=='CONFIGURE_PI_MAC', 'Wrong Pi'
serial=pathlib.Path('/proc/cpuinfo').read_text()
assert 'CONFIGURE_PI_SERIAL' in serial, 'Wrong Pi serial'
lock=(root/'specimen-lab.lock').open('a')
fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
for p in pathlib.Path('/proc').iterdir():
    if not p.name.isdigit() or int(p.name)==os.getpid(): continue
    try: command=(p/'cmdline').read_bytes().replace(b'\\x00',b' ')
    except OSError: continue
    if any(v in command for v in [b'openscan_motor_server.py',b'openscan_mjpeg_stream.py',b'rpicam-vid',b'rpicam-still']):
        raise RuntimeError('Existing scanner/camera owner; stop it normally first')
action=root/'experiment_captures'/ACTION_ID
action.mkdir(parents=True,exist_ok=False)
report={'action_id':ACTION_ID,'steps':MOVE_STEPS,'axis':MOVE_AXIS,'saved_limits':SAVED_LIMITS,'policy_version':POLICY_VERSION,'pose_kind':'commanded_relative_steps_uncalibrated','state':'started'}
def save(name):
    with (action/name).open('x') as out: json.dump(report,out)
save('intent.json')
link=None
try:
    if MOVE_STEPS:
        sys.path.insert(0,str(root))
        from openscan_motor_server import SerialLink
        link=SerialLink('/dev/serial/by-id/CONFIGURE_SCANNER_DEVICE',115200,6000)
        report['disable_before']=link.disable()
        assert 'OK DISABLE' in report['disable_before'],'No disabled acknowledgement'
        report['before']=link.status()
        before=positions(report['before'])
        if EXPECTED_POSITION is not None and before!=EXPECTED_POSITION:
            raise RuntimeError('Position changed since the last recorded action; inspect reference before moving')
        report['target']=check_target(before,MOVE_AXIS,MOVE_STEPS,SAVED_LIMITS)
        report['position_before']=before
        report['move']=link.move(MOVE_AXIS,MOVE_STEPS,speed=6000,disable=True)
        assert 'OK MOVE' in report['move'] and 'OK DISABLE' in report['move'],'Move/disable not acknowledged'
        assert not any(v.startswith(('ERR','WARN')) for v in report['move']),'Firmware rejected move'
        after=positions(report['move'])
        assert after==report['target'],'Unexpected commanded-position delta or other axis moved'
        report['after']=link.status()
        assert positions(report['after'])==after,'Position changed after disable'
        report['position_after']=after
        link.close(); link=None
        time.sleep(0.5)
    if TAKE_PHOTO:
        photo=action/'original.jpg'
        result=subprocess.run(['rpicam-still','-n','--width','3280','--height','2464','--quality','95','--timeout','1000','-o',str(photo)],capture_output=True,text=True,timeout=20)
        report['camera_exit']=result.returncode
        (action/'camera.stderr').write_text(result.stderr)
        assert result.returncode==0 and photo.stat().st_size>1000,'Capture failed'
        report['sha256']=hashlib.sha256(photo.read_bytes()).hexdigest()
        report['remote_image']=str(photo)
    report['take_photo']=TAKE_PHOTO
    report['state']='completed'
except BaseException as exc:
    report['state']='failed'; report['error']=str(exc)
finally:
    if link is not None:
        try: report['disable_final']=link.disable(); link.close()
        except Exception as exc: report['disable_error']=str(exc)
    save('terminal.json')
    print(json.dumps(report),flush=True)
if report['state']!='completed': sys.exit(1)
'''
    prefix = (Path(__file__).with_name("motion.py").read_text() + "\n" +
              f"ACTION_ID={action_id!r}\nMOVE_AXIS={axis!r}\nMOVE_STEPS={steps!r}\nSAVED_LIMITS={limits!r}\nEXPECTED_POSITION={expected_position!r}\nTAKE_PHOTO={take_photo!r}\n")
    return prefix + program


def reference_position(actions, calibration=None):
    expected=calibration['reference_position'] if calibration else None
    for past in sorted(actions,key=lambda p:p['created']):
        terminal=past['terminal']
        if terminal['state']!='completed':raise ValueError('An unresolved hardware action exists; inspect before moving')
        if calibration and datetime.fromisoformat(past['created'])<=datetime.fromisoformat(calibration['recorded_at']):continue
        report=terminal.get('remote_report',{})
        if report.get('after'):expected=positions(report['after'])
    return expected


def capture(store, specimen_id, view_id, *, attended=False, session_id="pilot-20260911", max_travel_steps=MAX_TRAVEL_STEPS,
            calibration=None,take_photo=True,cancel=None,pose_extra=None):
    if not session_id.replace("-", "").isalnum():
        raise ValueError("Invalid session ID")
    ledger = store.root / "hardware" / session_id
    ledger.mkdir(parents=True, exist_ok=True)
    with lease():
        if cancel is not None and cancel.is_set():raise ValueError('Demo stopped before next movement')
        previous = [json.loads(p.read_text()) for p in ledger.glob("*/intent.json")]
        for p in ledger.glob("*/intent.json"):
            final=p.with_name("terminal.json")
            if not final.exists() or json.loads(final.read_text())["state"] != "completed":
                raise ValueError("Prior hardware action is uncertain/failed; inspect it before any continuation")
        used = sum(abs(p["steps"]) for p in previous)
        axis, steps = check_request(view_id, used, attended, (store.root / "STOP").exists(),max_travel_steps)
        if calibration:
            limits=validate_limits(calibration['limits']);config_sha=calibration['sha256']
        else:limits, config_sha = saved_limits()
        snap=store.snapshot()
        if not any(s["id"]==specimen_id for s in snap["specimens"]):
            raise ValueError("Unknown specimen")
        expected_position=reference_position(snap['hardware_actions'],calibration)
        if calibration:check_target(expected_position,axis,steps,limits)
        action_id=uid("action")
        folder=ledger/action_id
        intent=dict(action_id=action_id,created=now(),specimen_id=specimen_id,view_id=view_id,steps=steps,
                    axis=axis,attended=True,used_steps_before=used,session_id=session_id,
                    policy_version=POLICY_VERSION,max_travel_steps=max_travel_steps,
                    saved_limits=limits,machine_state_sha256=None if calibration else config_sha,expected_position=expected_position,
                    limits_source='operator_calibration' if calibration else 'saved_machine_state',
                    take_photo=take_photo,calibration_sha256=calibration['sha256'] if calibration else None)
        write_new(folder/"intent.json",(canonical(intent)+"\n").encode())
        try:
            proc=subprocess.run([*SSH,"python3 -"],input=remote_program(action_id,axis,steps,limits,expected_position,take_photo),capture_output=True,text=True,timeout=40)
            write_new(folder/"ssh.stdout",proc.stdout.encode())
            write_new(folder/"ssh.stderr",proc.stderr.encode())
            if proc.returncode:
                raise ValueError("Remote action failed; inspect preserved SSH and Pi logs. Do not repeat.")
            report=json.loads(proc.stdout)
            if report.get("state")!="completed" or report.get("action_id")!=action_id:
                raise ValueError("Unexpected remote action report")
            if not take_photo:
                terminal=dict(state='completed',finished=now(),remote_report=report)
                write_new(folder/'terminal.json',(canonical(terminal)+'\n').encode())
                return dict(action_id=action_id,firmware_position=report.get('position_after'))
            # The path is generated here, never taken from a model or remote report.
            remote=f"/opt/openscan-mini/experiment_captures/{action_id}/original.jpg"
            fetched=subprocess.run([*SSH,"cat "+remote],capture_output=True,timeout=15)
            if fetched.returncode or hashlib.sha256(fetched.stdout).hexdigest()!=report["sha256"]:
                raise ValueError("Capture transfer/hash failed")
            image=store.add_image(specimen_id,fetched.stdout,pose={"kind":"commanded_relative_steps_uncalibrated",
                  "axis":axis,"relative_steps_this_action":steps,
                  "session_commanded_steps":{a:sum(p['steps'] for p in previous if p.get('axis','Y')==a)+(steps if axis==a else 0) for a in ('Y','Z')},
                  "firmware_position":report.get('position_after'),"saved_limits":limits,
                  "action_id":action_id,"session_id":session_id,"physical_angle_verified":False,**(pose_extra or {})})
            terminal=dict(state="completed",finished=now(),image_id=image["id"],remote_report=report)
        except Exception as exc:
            terminal=dict(state="uncertain",finished=now(),error=str(exc))
            write_new(folder/"terminal.json",(canonical(terminal)+"\n").encode())
            raise
        write_new(folder/"terminal.json",(canonical(terminal)+"\n").encode())
        return image
