"""Persistent Pi capture worker; launched over one private SSH stdin/stdout pipe.

motion.py is prepended by the launcher. This file has no hardware side effects
when imported; main() is called explicitly by the launcher after validation.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import select
import sys
import time


def emit(value):
    sys.stdout.write(json.dumps(value, allow_nan=False) + '\n')
    sys.stdout.flush()


def read_command(timeout=15):
    if not select.select([sys.stdin], [], [], timeout)[0]:
        raise TimeoutError('Idle capture connection expired')
    line = sys.stdin.buffer.readline(16000)
    if not line:
        return None
    if not line.endswith(b'\n'):
        raise ValueError('Oversized capture command')
    return json.loads(line)


def write_record(path, value):
    with path.open('x') as out:
        json.dump(value, out, allow_nan=False)


class Worker:
    def __init__(self, root, link, camera, limits, expected, max_steps):
        self.root, self.link, self.camera = root, link, camera
        self.limits, self.expected, self.max_steps = limits, expected, max_steps
        self.used = 0

    def action(self, command):
        required = {'op', 'action_id', 'axis', 'steps', 'expected_position', 'take_photo'}
        if set(command) != required or command['op'] != 'action':
            raise ValueError('Invalid action fields')
        action_id, axis, steps = command['action_id'], command['axis'], command['steps']
        if (not isinstance(action_id, str) or not action_id.startswith('action_')
                or not action_id[7:].isalnum() or type(steps) is not int
                or (axis, steps) not in VIEWS.values() or type(command['take_photo']) is not bool):
            raise ValueError('Invalid bounded action')
        if command['expected_position'] != self.expected or self.used + abs(steps) > self.max_steps:
            raise ValueError('Unexpected action reference or travel budget')
        target = check_target(self.expected, axis, steps, self.limits)
        folder = self.root / 'experiment_captures' / action_id
        folder.mkdir(parents=True, exist_ok=False)
        report = dict(action_id=action_id, axis=axis, steps=steps, state='started',
                      saved_limits=self.limits, target=target, take_photo=command['take_photo'],
                      capture_backend='persistent-picamera2-v1', started_monotonic=time.monotonic())
        write_record(folder / 'intent.json', report)
        raw = None
        try:
            report['disable_before'] = self.link.disable()
            if 'OK DISABLE' not in report['disable_before']:
                raise ValueError('Disable not acknowledged')
            report['before'] = self.link.status()
            before = positions(report['before'])
            if before != self.expected:
                raise ValueError('Live position changed; no move sent')
            report['position_before'] = before
            movement_started = time.monotonic()
            if steps:
                report['move'] = self.link.move(axis, steps, speed=6000, disable=True)
                if not {'OK MOVE', 'OK DISABLE'} <= set(report['move']):
                    raise ValueError('Move/disable not acknowledged')
                if any(line.startswith(('ERR', 'WARN')) for line in report['move']):
                    raise ValueError('Firmware rejected movement')
                if positions(report['move']) != target:
                    raise ValueError('Unexpected commanded-position delta')
            report['after'] = self.link.status()
            if positions(report['after']) != target:
                raise ValueError('Position changed after movement')
            report['position_after'] = target
            report['movement_wall_seconds'] = time.monotonic() - movement_started
            self.expected = target
            self.used += abs(steps)
            if command['take_photo']:
                # No settling delay on intermediate arc segments. Require a frame
                # whose exposure STARTED after the final movement and settling.
                time.sleep(.5)
                settled_ns = time.monotonic_ns()
                started = time.monotonic()
                request = self.camera.capture_request(flush=settled_ns)
                try:
                    metadata = request.get_metadata()
                    exposure_start = int(metadata['SensorTimestamp']) - 1000 * int(metadata['ExposureTime'])
                    if exposure_start < settled_ns:
                        raise ValueError('Camera returned a frame from before settling')
                    path = folder / 'original.jpg'
                    request.save('main', str(path))
                    report['camera_metadata'] = {key: float(metadata[key]) for key in
                        ('SensorTimestamp', 'ExposureTime', 'AnalogueGain', 'DigitalGain') if key in metadata}
                    report['settled_monotonic_ns'] = settled_ns
                    report['exposure_started_monotonic_ns'] = exposure_start
                finally:
                    request.release()
                raw = path.read_bytes()
                if len(raw) < 1000 or len(raw) > 12_000_000:
                    raise ValueError('Unexpected JPEG size')
                report['sha256'] = hashlib.sha256(raw).hexdigest()
                report['remote_image'] = str(path)
                report['camera_wall_seconds'] = time.monotonic() - started
            report['state'] = 'completed'
        except BaseException as exc:
            report.update(state='failed', error=str(exc))
            try:
                report['disable_on_error'] = self.link.disable()
            except Exception as disable_error:
                report['disable_error'] = str(disable_error)
        report['wall_seconds'] = time.monotonic() - report['started_monotonic']
        write_record(folder / 'terminal.json', report)
        response = {'report': report}
        if raw is not None and report['state'] == 'completed':
            response['image_base64'] = base64.b64encode(raw).decode()
        return response


def main():
    root = Path('/opt/openscan-mini')
    link = camera = lock = None
    try:
        if Path('/sys/class/net/eth0/address').read_text().strip() != 'CONFIGURE_PI_MAC':
            raise ValueError('Wrong Pi MAC')
        if 'CONFIGURE_PI_SERIAL' not in Path('/proc/cpuinfo').read_text():
            raise ValueError('Wrong Pi serial')
        lock = (root / 'specimen-lab.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit() or int(entry.name) == os.getpid():
                continue
            try:
                command = (entry / 'cmdline').read_bytes().replace(b'\x00', b' ')
            except OSError:
                continue
            if any(name in command for name in (b'openscan_motor_server.py', b'openscan_mjpeg_stream.py', b'rpicam-vid', b'rpicam-still')):
                raise ValueError('Existing scanner/camera owner')
        init = read_command()
        if not isinstance(init, dict) or set(init) != {'op', 'limits', 'expected_position', 'max_steps', 'serial_source_sha256'} or init['op'] != 'open':
            raise ValueError('Invalid session initialization')
        limits = validate_limits(init['limits'])
        check_target(init['expected_position'], None, 0, limits)
        travel_budget(init['max_steps'])
        source = root / 'openscan_motor_server.py'
        if hashlib.sha256(source.read_bytes()).hexdigest() != init['serial_source_sha256']:
            raise ValueError('Unexpected serial bridge source; inspect before capture')
        sys.path.insert(0, str(root))
        from openscan_motor_server import SerialLink
        from picamera2 import Picamera2
        link = SerialLink('/dev/serial/by-id/CONFIGURE_SCANNER_DEVICE', 115200, 6000)
        disabled = link.disable()
        position = positions(link.status())
        if 'OK DISABLE' not in disabled or position != init['expected_position']:
            raise ValueError('Initial disable/reference check failed')
        camera = Picamera2()
        config = camera.create_still_configuration(main={'size': (3280, 2464)}, raw=None,
                    buffer_count=2, queue=False, controls={'FrameDurationLimits': (100000, 1000000)})
        camera.configure(config)
        camera.options['quality'] = 95
        camera.start()
        time.sleep(1)
        emit({'state': 'ready', 'position': position, 'disable': disabled,
              'serial_source_sha256': init['serial_source_sha256'],
              'camera': {'backend': 'picamera2', 'size': [3280, 2464], 'quality': 95, 'queue': False}})
        worker = Worker(root, link, camera, limits, position, init['max_steps'])
        while True:
            command = read_command()
            if command is None:
                break
            if command == {'op': 'close'}:
                disabled = link.disable()
                if 'OK DISABLE' not in disabled:
                    raise ValueError('Final disable not acknowledged')
                camera.stop(); camera.close(); camera = None
                link.close(); link = None
                emit({'state': 'closed', 'disable': disabled})
                break
            response = worker.action(command)
            emit(response)
            if response['report']['state'] != 'completed':
                break
    except BaseException as exc:
        emit({'state': 'failed', 'error': str(exc)})
    finally:
        if link is not None:
            try:
                link.close()
            except Exception:
                pass
        if camera is not None:
            try:
                camera.stop(); camera.close()
            except Exception:
                pass
        if lock is not None:
            lock.close()
