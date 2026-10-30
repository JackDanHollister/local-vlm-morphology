"""One owned SSH/serial/camera session for a mapped scan; no motor retries."""
import base64
import hashlib
import json
import os
from pathlib import Path
import select
import subprocess
import time

from . import hardware
from .store import canonical, now, uid, write_new


class CaptureSession:
    def __init__(self, store, specimen_id, session_id, calibration, cancel):
        self.store, self.specimen_id, self.session_id = store, specimen_id, session_id
        self.calibration, self.cancel = calibration, cancel
        self.proc = None
        self.buffer = bytearray()
        self.folder = store.root / 'hardware_sessions' / session_id
        self.position = None
        self.used = 0
        self.lease = None
        self.stderr = None

    def __enter__(self):
        if self.cancel.is_set() or (self.store.root / 'STOP').exists():
            raise ValueError('Capture stopped before opening session')
        if not self.session_id.replace('-', '').isalnum():
            raise ValueError('Invalid session ID')
        snapshot = self.store.snapshot()
        if not any(s['id'] == self.specimen_id for s in snapshot['specimens']):
            raise ValueError('Unknown specimen')
        self.position = hardware.reference_position(snapshot['hardware_actions'], self.calibration)
        hardware.check_target(self.position, None, 0, self.calibration['limits'])
        self.lease = hardware.lease()
        self.lease.__enter__()
        try:
            self.folder.mkdir(parents=True, exist_ok=False)
            payload = (Path(__file__).with_name('motion.py').read_text() + '\n' +
                       Path(__file__).with_name('pi_worker.py').read_text() +
                       "\nemit({'state': 'bootstrapped'})\nmain()\n")
            write_new(self.folder / 'worker.py', payload.encode())
            self.opened = now()
            write_new(self.folder / 'request.json', (canonical(dict(created=self.opened,
                session_id=self.session_id, specimen_id=self.specimen_id, calibration=self.calibration,
                expected_position=self.position, worker_sha256=hashlib.sha256(payload.encode()).hexdigest(),
                backend='persistent-picamera2-v1', step_delay_us=6000, settle_seconds=.5)) + '\n').encode())
            self.stderr = (self.folder / 'stderr.log').open('xb')
            # Source is sent as data on stdin, so it never appears in process arguments.
            bootstrap = "python3 -u -c 'import sys,json; exec(compile(json.loads(sys.stdin.buffer.readline()), \"specimen-session-worker\", \"exec\"))'"
            self.proc = subprocess.Popen([*hardware.SSH, bootstrap], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=self.stderr, start_new_session=True)
            self.send(payload)
            if self.receive(15).get('state') != 'bootstrapped':
                raise ValueError('Worker bootstrap failed')
            serial_source = hardware.PROJECT / 'scripts' / 'openscan_motor_server.py'
            self.send(dict(op='open', limits=self.calibration['limits'], expected_position=self.position,
                           max_steps=2000, serial_source_sha256=hashlib.sha256(serial_source.read_bytes()).hexdigest()))
            ready = self.receive(25)
            write_new(self.folder / 'ready.json', (canonical(ready) + '\n').encode())
            if ready.get('state') != 'ready' or ready.get('position') != self.position:
                raise ValueError('Capture session initialization failed: ' + str(ready.get('error', 'reference mismatch')))
            return self
        except BaseException:
            self.close_transport()
            self.lease.__exit__(None, None, None)
            self.lease = None
            raise

    def send(self, packet):
        self.proc.stdin.write((canonical(packet) + '\n').encode())
        self.proc.stdin.flush()

    def receive(self, timeout):
        deadline = time.monotonic() + timeout
        while b'\n' not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Capture worker response timed out; no retry')
            if not select.select([self.proc.stdout], [], [], min(.25, remaining))[0]:
                continue
            chunk = os.read(self.proc.stdout.fileno(), 65536)
            if not chunk:
                raise ValueError('Capture worker connection ended; inspect session logs')
            self.buffer.extend(chunk)
            if len(self.buffer) > 18_000_000:
                raise ValueError('Capture worker exceeded response bound')
        line, _, remaining = self.buffer.partition(b'\n')
        self.buffer = bytearray(remaining)
        response = json.loads(line)
        if not isinstance(response, dict):
            raise ValueError('Invalid worker response')
        return response

    def capture(self, store, specimen_id, view_id, *, attended, session_id,
                max_travel_steps, calibration, take_photo, cancel, pose_extra):
        if (store is not self.store or specimen_id != self.specimen_id or session_id != self.session_id
                or calibration != self.calibration or max_travel_steps != 2000):
            raise ValueError('Capture session identity mismatch')
        axis, steps = hardware.check_request(view_id, self.used, attended,
            cancel.is_set() or (store.root / 'STOP').exists(), max_travel_steps)
        recorded = hardware.reference_position(store.snapshot()['hardware_actions'], calibration)
        if recorded != self.position:
            raise ValueError('Recorded position changed outside the capture session')
        target = hardware.check_target(self.position, axis, steps, calibration['limits'])
        action_id = uid('action')
        folder = store.root / 'hardware' / session_id / action_id
        intent = dict(action_id=action_id, created=now(), specimen_id=specimen_id, session_id=session_id,
            axis=axis, steps=steps, view_id=view_id, attended=True, used_steps_before=self.used,
            max_travel_steps=max_travel_steps, expected_position=self.position, saved_limits=calibration['limits'],
            limits_source='operator_calibration', calibration_sha256=calibration['sha256'],
            policy_version='attended-mixed-steps-v2', take_photo=take_photo, capture_backend='persistent-picamera2-v1')
        write_new(folder / 'intent.json', (canonical(intent) + '\n').encode())
        try:
            self.send(dict(op='action', action_id=action_id, axis=axis, steps=steps,
                           expected_position=self.position, take_photo=take_photo))
            response = self.receive(30)
            report = response.get('report', response)
            write_new(folder / 'remote-report.json', (canonical(report) + '\n').encode())
            if report.get('state') != 'completed' or report.get('action_id') != action_id:
                raise ValueError('Remote action failed: ' + str(report.get('error', 'invalid report')))
            if report.get('position_after') != target or hardware.positions(report['after']) != target:
                raise ValueError('Remote target mismatch')
            if take_photo:
                raw = base64.b64decode(response['image_base64'], validate=True)
                if hashlib.sha256(raw).hexdigest() != report['sha256']:
                    raise ValueError('Transferred photo hash mismatch')
                if report['exposure_started_monotonic_ns'] < report['settled_monotonic_ns']:
                    raise ValueError('Photo predates settling')
                image = store.add_image(specimen_id, raw, pose=dict(kind='commanded_relative_steps_uncalibrated',
                    axis=axis, relative_steps_this_action=steps, firmware_position=target,
                    saved_limits=calibration['limits'], action_id=action_id, session_id=session_id,
                    physical_angle_verified=False, capture_backend='persistent-picamera2-v1', **pose_extra))
                if (image['width'], image['height']) != (3280, 2464):
                    raise ValueError('Unexpected capture dimensions')
                terminal = dict(state='completed', finished=now(), image_id=image['id'], remote_report=report)
                result = image
            else:
                terminal = dict(state='completed', finished=now(), remote_report=report)
                result = dict(action_id=action_id, firmware_position=target)
            write_new(folder / 'terminal.json', (canonical(terminal) + '\n').encode())
            self.position, self.used = target, self.used + abs(steps)
            return result
        except BaseException as exc:
            write_new(folder / 'terminal.json', (canonical(dict(state='uncertain', finished=now(), error=str(exc))) + '\n').encode())
            raise

    def close_transport(self):
        if self.proc is not None:
            if self.proc.stdin and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill(); self.proc.wait()
            if self.proc.stdout:
                self.proc.stdout.close()
        if self.stderr:
            self.stderr.close()

    def __exit__(self, exc_type, exc, tb):
        error = None
        try:
            self.send({'op': 'close'})
            reply = self.receive(8)
            if reply.get('state') != 'closed' or 'OK DISABLE' not in reply.get('disable', []):
                raise ValueError('Capture session close/disable not acknowledged')
            write_new(self.folder / 'terminal.json', (canonical(dict(state='closed', finished=now(), reply=reply,
                final_position=self.position, travel_steps=self.used, body_error=str(exc) if exc else None)) + '\n').encode())
        except Exception as close_error:
            error = close_error
            write_new(self.folder / 'terminal.json', (canonical(dict(state='close_error', finished=now(), error=str(close_error))) + '\n').encode())
        finally:
            self.close_transport()
            self.lease.__exit__(exc_type, exc, tb)
        if error and exc is None:
            raise error
