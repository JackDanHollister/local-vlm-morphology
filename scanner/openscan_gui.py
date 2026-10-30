#!/usr/bin/env python3
"""OpenScan Mini control GUI.

Starts a Pi_B camera stream and a persistent Arduino motor bridge, then offers
camera preview, Y/Z jogs, soft stops, and a simple image-capture scan routine.
"""

from __future__ import annotations

import csv
import io
import json
import os
import queue
import shlex
import socket
import subprocess
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from urllib.request import urlopen

from PIL import Image, ImageTk


ROOT = Path(__file__).resolve().parent
STATE_PATH = ROOT / "config" / "machine_state.json"
PI_IP = os.environ.get("OPENSCAN_PI_HOST", "192.0.2.10")
PI_USER = os.environ.get("OPENSCAN_PI_USER", "scanner")
SSH_KEY = os.path.expanduser(os.environ.get("OPENSCAN_SSH_KEY", "~/.ssh/openscan_key"))
SSH_CTRL = os.environ.get("OPENSCAN_SSH_CTRL", "/tmp/openscan_mini_ctrl")
REMOTE_DIR = os.environ.get("OPENSCAN_REMOTE_DIR", "/opt/openscan-mini")
STREAM_PORT = int(os.environ.get("OPENSCAN_STREAM_PORT", "8081"))
MOTOR_PORT = int(os.environ.get("OPENSCAN_MOTOR_PORT", "10002"))
DEFAULT_SPEED = int(os.environ.get("OPENSCAN_STEP_DELAY_US", "6000"))
SNAPSHOT_TIMEOUT = float(os.environ.get("OPENSCAN_SNAPSHOT_TIMEOUT", "20"))
ACTIVE_AXES = ("Y", "Z")
TURNTABLE_AXIS = os.environ.get("OPENSCAN_TURNTABLE_AXIS", "Y")
ROTOR_AXIS = os.environ.get("OPENSCAN_ROTOR_AXIS", "Z")
TURNTABLE_ROTARY = os.environ.get("OPENSCAN_TURNTABLE_ROTARY", "1").lower() not in {"0", "false", "no", "off"}
STEPS_PER_MM = 80.0
JOG_STEPS = (1, 5, 10, 50, 100)
DEFAULT_SCAN_FRAMES = 36
DEFAULT_SCAN_BANDS = 3
DEFAULT_SCAN_SETTLE = 0.8

STREAM_W = 720
STREAM_H = 540

BG = "#202225"
PANEL = "#2b2f34"
PANEL_2 = "#30363d"
TXT = "#edf2f4"
MUTED = "#9aa4ad"
ACC = "#2f81f7"
OK = "#42b883"
WARN = "#d29922"
BAD = "#f85149"


SSH_BASE = [
    "ssh",
    "-i",
    SSH_KEY,
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "BatchMode=yes",
    "-o",
    f"ControlPath={SSH_CTRL}",
]


def run_ssh(command: str, timeout: int = 20) -> str:
    result = subprocess.run(
        SSH_BASE + ["-o", "ControlMaster=no", f"{PI_USER}@{PI_IP}", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip())
    return result.stdout.strip()


def start_master() -> subprocess.Popen:
    try:
        os.remove(SSH_CTRL)
    except FileNotFoundError:
        pass
    proc = subprocess.Popen(
        SSH_BASE
        + [
            "-o",
            "ControlMaster=yes",
            "-o",
            "ControlPersist=yes",
            "-N",
            f"{PI_USER}@{PI_IP}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(1.5)
    return proc


def stop_master() -> None:
    subprocess.run(SSH_BASE + ["-O", "stop", f"{PI_USER}@{PI_IP}"], capture_output=True, timeout=5)


def scp_to_pi(local_path: Path, remote_path: str) -> None:
    subprocess.run(
        [
            "scp",
            "-i",
            SSH_KEY,
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "BatchMode=yes",
            str(local_path),
            f"{PI_USER}@{PI_IP}:{remote_path}",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )


class MotorClient:
    def __init__(self, host: str, port: int) -> None:
        self._sock = socket.create_connection((host, port), timeout=5)
        self._sock.settimeout(30)
        self._lock = threading.Lock()

    def request(self, line: str) -> list[str]:
        with self._lock:
            self._sock.sendall((line + "\n").encode())
            data = b""
            while b"\nEND\n" not in data and not data.endswith(b"END\n"):
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise RuntimeError("motor server disconnected")
                data += chunk
            lines = [l.strip() for l in data.decode(errors="replace").splitlines()]
            return [l for l in lines if l and l != "END"]

    def status(self) -> dict[str, float]:
        return parse_status(self.request("status"))

    def move(self, axis: str, steps: int, speed: int, hold: bool = False) -> dict[str, float]:
        hold_arg = " hold" if hold else ""
        return parse_status(self.request(f"move {axis} {steps} {speed}{hold_arg}"))

    def zero(self, axis: str) -> dict[str, float]:
        return parse_status(self.request(f"zero {axis}") + self.request("status"))

    def limits(self, axis: str, min_mm: float, max_mm: float) -> list[str]:
        return self.request(f"limits {axis} {min_mm:g} {max_mm:g}")

    def disable(self) -> None:
        try:
            self.request("disable")
        except Exception:
            pass

    def close(self, send_quit: bool = True) -> None:
        if send_quit:
            try:
                self.request("quit")
            except Exception:
                pass
        self._sock.close()


def parse_status(lines: list[str]) -> dict[str, float]:
    for line in reversed(lines):
        if line.startswith("STATUS"):
            out: dict[str, float] = {}
            for item in line.split()[1:]:
                if "=" in item:
                    axis, val = item.split("=", 1)
                    out[axis] = float(val)
            return out
    return {}


class AxisState:
    def __init__(self, axis: str) -> None:
        self.axis = axis
        self.position = 0.0
        self.min_stop: float | None = None
        self.max_stop: float | None = None

    def target_allowed(self, step_delta: int) -> bool:
        target = self.position + step_delta / STEPS_PER_MM
        if self.min_stop is not None and target < self.min_stop:
            return False
        if self.max_stop is not None and target > self.max_stop:
            return False
        return True


@dataclass(frozen=True)
class ScanPoint:
    index: int
    band: int
    frame: int
    targets: dict[str, float]


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("OpenScan Mini Control")
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self.close)

        self.running = True
        self.connected = False
        self.scan_stop = threading.Event()
        self.move_lock = threading.Lock()
        self.reconnect_lock = threading.Lock()
        self.move_seq = 0
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.ssh_proc: subprocess.Popen | None = None
        self.motor: MotorClient | None = None
        self.latest_photo: ImageTk.PhotoImage | None = None
        self.latest_jpeg: bytes | None = None
        self.axis_state = {axis: AxisState(axis) for axis in ACTIVE_AXES}
        self.motion_buttons: list[ttk.Button] = []

        self.speed_var = tk.IntVar(value=DEFAULT_SPEED)
        self.hold_motors_var = tk.BooleanVar(value=True)
        self.scan_axis_var = tk.StringVar(value=TURNTABLE_AXIS)
        self.scan_frames_var = tk.IntVar(value=DEFAULT_SCAN_FRAMES)
        self.scan_bands_var = tk.IntVar(value=DEFAULT_SCAN_BANDS)
        self.scan_step_var = tk.IntVar(value=200)
        self.scan_settle_var = tk.DoubleVar(value=DEFAULT_SCAN_SETTLE)

        self.status_var = tk.StringVar(value="Starting...")
        self.axis_widgets: dict[str, dict[str, tk.Variable | ttk.Label]] = {}

        self._load_machine_state()
        self._build_ui()
        self.after(100, self._drain_queue)
        threading.Thread(target=self._connect, daemon=True).start()

    def _build_ui(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=TXT)
        style.configure("Panel.TLabel", background=PANEL, foreground=TXT)
        style.configure("Muted.TLabel", background=PANEL, foreground=MUTED)
        style.configure("Panel.TCheckbutton", background=PANEL, foreground=TXT)
        style.configure("TButton", padding=(8, 4))

        root = ttk.Frame(self, padding=10)
        root.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        self.canvas = tk.Canvas(root, width=STREAM_W, height=STREAM_H, bg="#050505", highlightthickness=0)
        self.canvas.grid(row=0, column=0, rowspan=3, sticky="nsew")
        self.canvas.create_text(STREAM_W // 2, STREAM_H // 2, text="Waiting for stream", fill=MUTED)

        side = ttk.Frame(root, style="Panel.TFrame", padding=10)
        side.grid(row=0, column=1, sticky="ns", padx=(10, 0))

        ttk.Label(side, text="Motors", style="Panel.TLabel", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        ttk.Label(side, text="step delay us", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 2))
        ttk.Entry(side, textvariable=self.speed_var, width=8).grid(row=1, column=1, sticky="e", pady=(8, 2))
        ttk.Checkbutton(
            side,
            text="Hold motors after jog",
            variable=self.hold_motors_var,
            style="Panel.TCheckbutton",
        ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(4, 6))

        row = 3
        for axis in ACTIVE_AXES:
            self._build_axis_panel(side, axis, row)
            row += 1

        scan = ttk.Frame(side, style="Panel.TFrame", padding=(0, 10, 0, 0))
        scan.grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Label(scan, text="Scan", style="Panel.TLabel", font=("TkDefaultFont", 12, "bold")).grid(
            row=0, column=0, columnspan=4, sticky="w"
        )
        ttk.Label(scan, text="Y frames", style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        ttk.Entry(scan, textvariable=self.scan_frames_var, width=6).grid(row=1, column=1, sticky="ew")
        ttk.Label(scan, text="Z bands", style="Muted.TLabel").grid(row=1, column=2, sticky="w", padx=(8, 0))
        ttk.Entry(scan, textvariable=self.scan_bands_var, width=6).grid(row=1, column=3, sticky="e")
        ttk.Label(scan, text="Y step", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(scan, textvariable=self.scan_step_var, width=6).grid(row=2, column=1, sticky="ew", pady=(6, 0))
        ttk.Label(scan, text="settle", style="Muted.TLabel").grid(row=2, column=2, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Entry(scan, textvariable=self.scan_settle_var, width=6).grid(row=2, column=3, sticky="e", pady=(6, 0))
        scan_start = ttk.Button(scan, text="Start Scan", command=self.start_scan)
        scan_start.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.motion_buttons.append(scan_start)
        ttk.Button(scan, text="Stop", command=self.scan_stop.set).grid(row=3, column=2, columnspan=2, sticky="ew", padx=(8, 0), pady=(10, 0))

        bottom = ttk.Frame(root, padding=(0, 8, 0, 0))
        bottom.grid(row=3, column=0, columnspan=2, sticky="ew")
        ttk.Button(bottom, text="Refresh Status", command=self.refresh_status).pack(side="left")
        ttk.Button(bottom, text="Disable Drivers", command=self.disable_drivers).pack(side="left", padx=(8, 0))
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=(12, 0))
        self._set_motion_controls(False)

    def _build_axis_panel(self, parent: ttk.Frame, axis: str, row: int) -> None:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=(0, 10, 0, 6))
        frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        ttk.Label(frame, text=f"Axis {axis}", style="Panel.TLabel", font=("TkDefaultFont", 10, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        pos_var = tk.StringVar(value="0.000 mm")
        min_var = tk.StringVar(value="min none")
        max_var = tk.StringVar(value="max none")
        direct_var = tk.IntVar(value=100)
        ttk.Label(frame, textvariable=pos_var, style="Panel.TLabel").grid(row=0, column=1, columnspan=4, sticky="e")

        col = 0
        for step in JOG_STEPS:
            btn = ttk.Button(frame, text=f"-{step}", width=5, command=lambda s=step, a=axis: self.move_axis(a, -s))
            btn.grid(row=1, column=col, padx=1, pady=(6, 0))
            self.motion_buttons.append(btn)
            col += 1
        col = 0
        for step in JOG_STEPS:
            btn = ttk.Button(frame, text=f"+{step}", width=5, command=lambda s=step, a=axis: self.move_axis(a, s))
            btn.grid(row=2, column=col, padx=1, pady=(4, 0))
            self.motion_buttons.append(btn)
            col += 1

        ttk.Label(frame, textvariable=min_var, style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Label(frame, textvariable=max_var, style="Muted.TLabel").grid(row=3, column=1, sticky="w", pady=(6, 0))
        ttk.Button(frame, text="Set Min", command=lambda a=axis: self.set_stop(a, "min")).grid(row=4, column=0, sticky="ew", pady=(4, 0))
        ttk.Button(frame, text="Set Max", command=lambda a=axis: self.set_stop(a, "max")).grid(row=4, column=1, sticky="ew", pady=(4, 0))
        ttk.Button(frame, text="Clear", command=lambda a=axis: self.clear_stops(a)).grid(row=4, column=2, sticky="ew", pady=(4, 0))
        zero_btn = ttk.Button(frame, text="Zero", command=lambda a=axis: self.zero_axis(a))
        zero_btn.grid(row=4, column=3, sticky="ew", pady=(4, 0))
        self.motion_buttons.append(zero_btn)

        go_min_btn = ttk.Button(frame, text="Go Min", command=lambda a=axis: self.move_to_stop(a, "min"))
        go_min_btn.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        self.motion_buttons.append(go_min_btn)
        go_max_btn = ttk.Button(frame, text="Go Max", command=lambda a=axis: self.move_to_stop(a, "max"))
        go_max_btn.grid(row=5, column=1, sticky="ew", pady=(6, 0))
        self.motion_buttons.append(go_max_btn)

        ttk.Entry(frame, textvariable=direct_var, width=7).grid(row=6, column=0, sticky="ew", pady=(6, 0))
        move_btn = ttk.Button(frame, text="Move", command=lambda a=axis, v=direct_var: self.move_axis(a, int(v.get())))
        move_btn.grid(row=6, column=1, sticky="ew", pady=(6, 0))
        self.motion_buttons.append(move_btn)
        move_neg_btn = ttk.Button(frame, text="Move -", command=lambda a=axis, v=direct_var: self.move_axis(a, -int(v.get())))
        move_neg_btn.grid(row=6, column=2, sticky="ew", pady=(6, 0))
        self.motion_buttons.append(move_neg_btn)

        self.axis_widgets[axis] = {
            "pos": pos_var,
            "min": min_var,
            "max": max_var,
            "direct": direct_var,
        }

    def _connect(self) -> None:
        try:
            self._set_status("Starting SSH session")
            self.ssh_proc = start_master()
            self._set_status("Uploading Pi services")
            run_ssh(f"mkdir -p {shlex.quote(REMOTE_DIR)}")
            scp_to_pi(ROOT / "scripts" / "openscan_motor_server.py", f"{REMOTE_DIR}/openscan_motor_server.py")
            scp_to_pi(ROOT / "scripts" / "openscan_mjpeg_stream.py", f"{REMOTE_DIR}/openscan_mjpeg_stream.py")

            self._set_status("Starting Pi services")
            run_ssh(
                "pkill -f '^python3 .*/openscan_motor_server.py' || true; "
                "pkill -f '^python3 .*/openscan_mjpeg_stream.py' || true",
                timeout=5,
            )
            run_ssh(f"chmod +x {shlex.quote(REMOTE_DIR)}/openscan_*.py", timeout=5)
            run_ssh(
                f"nohup python3 {shlex.quote(REMOTE_DIR)}/openscan_motor_server.py "
                f"--port {MOTOR_PORT} --speed {DEFAULT_SPEED} "
                f">{shlex.quote(REMOTE_DIR)}/motor.log 2>&1 </dev/null &",
                timeout=5,
            )
            run_ssh(
                f"nohup python3 {shlex.quote(REMOTE_DIR)}/openscan_mjpeg_stream.py "
                f"--port {STREAM_PORT} "
                f">{shlex.quote(REMOTE_DIR)}/stream.log 2>&1 </dev/null &",
                timeout=5,
            )
            time.sleep(3)

            self._set_status("Connecting motor server")
            for _ in range(10):
                try:
                    self.motor = MotorClient(PI_IP, MOTOR_PORT)
                    break
                except OSError:
                    time.sleep(1)
            if self.motor is None:
                raise RuntimeError("could not connect to motor server")

            self.connected = True
            self._push_all_limits()
            self.ui_queue.put(("motion_controls", True))
            self.refresh_status()
            threading.Thread(target=self._stream_loop, daemon=True).start()
            threading.Thread(target=self._status_loop, daemon=True).start()
            self._set_status("Connected")
        except Exception as exc:
            self.ui_queue.put(("motion_controls", False))
            self._set_status(f"Connection failed: {exc}")

    def _mark_motor_disconnected(self, reason: str) -> None:
        self.connected = False
        motor = self.motor
        self.motor = None
        if motor is not None:
            try:
                motor.close(send_quit=False)
            except Exception:
                pass
        self.ui_queue.put(("motion_controls", False))
        self._set_status(f"{reason}; reconnecting motor")
        self._start_motor_reconnect()

    def _start_motor_reconnect(self) -> None:
        if self.reconnect_lock.locked():
            return
        threading.Thread(target=self._reconnect_motor, daemon=True).start()

    def _reconnect_motor(self) -> None:
        if not self.reconnect_lock.acquire(blocking=False):
            return
        try:
            for _ in range(10):
                if not self.running:
                    return
                try:
                    self.motor = MotorClient(PI_IP, MOTOR_PORT)
                    self.connected = True
                    self._push_all_limits()
                    self.ui_queue.put(("motion_controls", True))
                    self.refresh_status()
                    self._set_status("Motor reconnected")
                    return
                except OSError:
                    time.sleep(1)
            self._set_status("Motor reconnect failed")
        finally:
            self.reconnect_lock.release()

    def _stream_loop(self) -> None:
        url = f"http://{PI_IP}:{STREAM_PORT}/stream"
        while self.running:
            try:
                req = urlopen(url, timeout=10)
                buf = b""
                while self.running:
                    chunk = req.read(4096)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        start = buf.find(b"\xff\xd8")
                        if start == -1:
                            buf = b""
                            break
                        end = buf.find(b"\xff\xd9", start + 2)
                        if end == -1:
                            buf = buf[start:]
                            break
                        frame = buf[start : end + 2]
                        buf = buf[end + 2 :]
                        self.latest_jpeg = frame
                        image = Image.open(io.BytesIO(frame))
                        image.thumbnail((STREAM_W, STREAM_H), Image.LANCZOS)
                        bg = Image.new("RGB", (STREAM_W, STREAM_H), "#050505")
                        x = (STREAM_W - image.width) // 2
                        y = (STREAM_H - image.height) // 2
                        bg.paste(image, (x, y))
                        photo = ImageTk.PhotoImage(bg)
                        self.ui_queue.put(("frame", photo))
            except Exception:
                time.sleep(1)

    def _capture_scan_frame(self) -> bytes:
        url = f"http://{PI_IP}:{STREAM_PORT}/snapshot"
        with urlopen(url, timeout=SNAPSHOT_TIMEOUT) as req:
            frame = req.read()
        if not frame.startswith(b"\xff\xd8") or not frame.endswith(b"\xff\xd9"):
            raise RuntimeError("snapshot endpoint did not return a JPEG")
        return frame

    def _status_loop(self) -> None:
        while self.running:
            time.sleep(2)
            if self.connected:
                self.refresh_status()

    def _drain_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "frame":
                    self.latest_photo = payload  # type: ignore[assignment]
                    self.canvas.delete("all")
                    self.canvas.create_image(0, 0, anchor="nw", image=self.latest_photo)
                elif kind == "status":
                    self.status_var.set(str(payload))
                elif kind == "positions":
                    self._apply_positions(payload)  # type: ignore[arg-type]
                elif kind == "motion_controls":
                    self._set_motion_controls(bool(payload))
        except queue.Empty:
            pass
        if self.running:
            self.after(100, self._drain_queue)

    def _set_status(self, text: str) -> None:
        print(text, flush=True)
        self.ui_queue.put(("status", text))

    def _load_machine_state(self) -> None:
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            self._set_status(f"Could not load machine state: {exc}")
            return

        axes = data.get("axes", {})
        for axis, state in self.axis_state.items():
            axis_data = axes.get(axis, {})
            state.min_stop = axis_data.get("min_stop")
            state.max_stop = axis_data.get("max_stop")
            state.position = float(axis_data.get("position") or 0.0)

        scan = data.get("scan", {})
        if scan:
            self.scan_axis_var.set(scan.get("axis", TURNTABLE_AXIS))
            self.scan_frames_var.set(int(scan.get("frames", DEFAULT_SCAN_FRAMES)))
            self.scan_bands_var.set(int(scan.get("bands", DEFAULT_SCAN_BANDS)))
            self.scan_step_var.set(int(scan.get("step", self.scan_step_var.get())))
            self.scan_settle_var.set(float(scan.get("settle", DEFAULT_SCAN_SETTLE)))
        else:
            self._configure_scan_from_limits()

    def _save_machine_state(self) -> None:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "axes": {
                axis: {
                    "min_stop": state.min_stop,
                    "max_stop": state.max_stop,
                    "position": state.position,
                }
                for axis, state in self.axis_state.items()
            },
            "scan": {
                "axis": self.scan_axis_var.get(),
                "turntable_axis": TURNTABLE_AXIS,
                "rotor_axis": ROTOR_AXIS,
                "frames": int(self.scan_frames_var.get()),
                "bands": int(self.scan_bands_var.get()),
                "step": int(self.scan_step_var.get()),
                "settle": float(self.scan_settle_var.get()),
            },
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(STATE_PATH)

    def _set_motion_controls(self, enabled: bool) -> None:
        state = "normal" if enabled and self.connected else "disabled"
        for button in self.motion_buttons:
            button.configure(state=state)

    def refresh_status(self) -> None:
        if not self.motor:
            return
        if self.move_lock.locked():
            return

        def worker() -> None:
            try:
                self.ui_queue.put(("positions", self.motor.status()))
            except Exception as exc:
                self._mark_motor_disconnected(f"Status failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _store_positions(self, positions: dict[str, float]) -> None:
        for axis, pos in positions.items():
            if axis in self.axis_state:
                self.axis_state[axis].position = pos

    def _apply_positions(self, positions: dict[str, float]) -> None:
        self._store_positions(positions)
        for axis, pos in positions.items():
            if axis in self.axis_state:
                var = self.axis_widgets[axis]["pos"]
                assert isinstance(var, tk.StringVar)
                var.set(f"{pos:.3f} mm")
        self._save_machine_state()

    def move_axis(self, axis: str, steps: int) -> None:
        if not self.motor:
            self._set_status("Motor server is not connected")
            return
        try:
            speed = int(self.speed_var.get())
        except (tk.TclError, ValueError):
            self._set_status("Move failed: speed must be an integer")
            return
        state = self.axis_state[axis]
        if not state.target_allowed(steps):
            self._set_status(f"Axis {axis}: blocked by soft stop")
            return
        if not self.move_lock.acquire(blocking=False):
            self._set_status("Move already in progress")
            return
        hold = bool(self.hold_motors_var.get())
        self.move_seq += 1
        move_id = self.move_seq
        self._set_motion_controls(False)
        self._set_status(f"Move {move_id}: {axis} {steps:+d} steps")

        def worker() -> None:
            try:
                pos = self.motor.move(axis, steps, speed, hold=hold)
                self.ui_queue.put(("positions", pos))
                self._set_status(f"Move {move_id}: done {axis} {steps:+d} steps")
            except Exception as exc:
                self._set_status(f"Move {move_id}: failed: {exc}")
                self._mark_motor_disconnected(f"Move {move_id}: motor connection lost")
            finally:
                self.move_lock.release()
                self.ui_queue.put(("motion_controls", True))

        threading.Thread(target=worker, daemon=True).start()

    def set_stop(self, axis: str, side: str) -> None:
        state = self.axis_state[axis]
        if side == "min":
            state.min_stop = state.position
        else:
            state.max_stop = state.position
        self._configure_scan_from_limits()
        self._save_machine_state()
        self._push_limits(axis)
        self._update_stop_labels(axis)

    def clear_stops(self, axis: str) -> None:
        state = self.axis_state[axis]
        state.min_stop = None
        state.max_stop = None
        self._save_machine_state()
        self._push_limits(axis)
        self._update_stop_labels(axis)

    def move_to_stop(self, axis: str, side: str) -> None:
        state = self.axis_state[axis]
        target = state.min_stop if side == "min" else state.max_stop
        if target is None:
            self._set_status(f"Axis {axis}: {side} stop is not set")
            return

        steps = round((target - state.position) * STEPS_PER_MM)
        if steps == 0:
            self._set_status(f"Axis {axis}: already at {side} stop")
            return
        self.move_axis(axis, steps)

    def _push_all_limits(self) -> None:
        for axis in ACTIVE_AXES:
            self._push_limits(axis)
            self._update_stop_labels(axis)

    def _push_limits(self, axis: str) -> None:
        if not self.motor:
            return
        state = self.axis_state[axis]
        min_mm = state.min_stop if state.min_stop is not None else -1000.0
        max_mm = state.max_stop if state.max_stop is not None else 1000.0

        def worker() -> None:
            try:
                motor = self.motor
                if motor:
                    motor.limits(axis, min_mm, max_mm)
            except Exception as exc:
                self._set_status(f"Limit update failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _update_stop_labels(self, axis: str) -> None:
        state = self.axis_state[axis]
        min_var = self.axis_widgets[axis]["min"]
        max_var = self.axis_widgets[axis]["max"]
        assert isinstance(min_var, tk.StringVar)
        assert isinstance(max_var, tk.StringVar)
        min_var.set("min none" if state.min_stop is None else f"min {state.min_stop:.3f}")
        max_var.set("max none" if state.max_stop is None else f"max {state.max_stop:.3f}")

    def zero_axis(self, axis: str) -> None:
        if not self.motor:
            return
        if not self.move_lock.acquire(blocking=False):
            self._set_status("Move already in progress")
            return
        self._set_motion_controls(False)

        def worker() -> None:
            try:
                self.ui_queue.put(("positions", self.motor.zero(axis)))
                self._set_status(f"Axis {axis}: zeroed")
            except Exception as exc:
                self._set_status(f"Zero failed: {exc}")
                self._mark_motor_disconnected("Zero failed because motor connection was lost")
            finally:
                self.move_lock.release()
                self.ui_queue.put(("motion_controls", True))

        threading.Thread(target=worker, daemon=True).start()

    def disable_drivers(self) -> None:
        if self.motor:
            threading.Thread(target=self.motor.disable, daemon=True).start()
            self._set_status("Drivers disabled")

    def _configure_scan_from_limits(self) -> bool:
        if TURNTABLE_AXIS not in self.axis_state or ROTOR_AXIS not in self.axis_state:
            return False
        turntable = self.axis_state[TURNTABLE_AXIS]
        rotor = self.axis_state[ROTOR_AXIS]
        for state in (turntable, rotor):
            if state.min_stop is None or state.max_stop is None:
                return False
            if state.max_stop <= state.min_stop:
                return False

        frames = max(2, int(self.scan_frames_var.get() or DEFAULT_SCAN_FRAMES))
        bands = max(1, int(self.scan_bands_var.get() or DEFAULT_SCAN_BANDS))
        tolerance = 1.0 / STEPS_PER_MM
        for state in (turntable, rotor):
            if state.position < state.min_stop - tolerance or state.position > state.max_stop + tolerance:
                return False

        y_range_steps = max(1, round((turntable.max_stop - turntable.min_stop) * STEPS_PER_MM))
        first_y_from_min = abs(turntable.position - turntable.min_stop) <= abs(turntable.position - turntable.max_stop)
        step_divisor = frames if TURNTABLE_ROTARY else frames - 1
        if first_y_from_min:
            step = max(1, round(y_range_steps / step_divisor))
        else:
            step = -max(1, round(y_range_steps / step_divisor))

        self.scan_axis_var.set(TURNTABLE_AXIS)
        self.scan_frames_var.set(frames)
        self.scan_bands_var.set(bands)
        self.scan_step_var.set(step)
        if float(self.scan_settle_var.get()) <= 0:
            self.scan_settle_var.set(DEFAULT_SCAN_SETTLE)
        return True

    @staticmethod
    def _linspace(start: float, end: float, count: int) -> list[float]:
        if count <= 1:
            return [start]
        return [start + (end - start) * i / (count - 1) for i in range(count)]

    @staticmethod
    def _rotary_positions(minimum: float, maximum: float, count: int, start_from_min: bool) -> list[float]:
        span = maximum - minimum
        if start_from_min:
            return [minimum + span * i / count for i in range(count)]
        return [maximum - span * i / count for i in range(count)]

    def _build_scan_path(self) -> list[ScanPoint]:
        frames = max(2, int(self.scan_frames_var.get() or DEFAULT_SCAN_FRAMES))
        bands = max(1, int(self.scan_bands_var.get() or DEFAULT_SCAN_BANDS))
        turntable = self.axis_state[TURNTABLE_AXIS]
        rotor = self.axis_state[ROTOR_AXIS]
        assert turntable.min_stop is not None and turntable.max_stop is not None
        assert rotor.min_stop is not None and rotor.max_stop is not None

        first_y_from_min = abs(turntable.position - turntable.min_stop) <= abs(turntable.position - turntable.max_stop)
        if TURNTABLE_ROTARY:
            y_forward = self._rotary_positions(turntable.min_stop, turntable.max_stop, frames, first_y_from_min)
        else:
            y_forward = self._linspace(turntable.min_stop, turntable.max_stop, frames)
            if not first_y_from_min:
                y_forward = list(reversed(y_forward))
        y_reverse = list(reversed(y_forward))

        z_from_min = abs(rotor.position - rotor.min_stop) <= abs(rotor.position - rotor.max_stop)
        z_positions = self._linspace(rotor.min_stop, rotor.max_stop, bands)
        if not z_from_min:
            z_positions.reverse()

        path: list[ScanPoint] = []
        index = 1
        for band_index, z_target in enumerate(z_positions, start=1):
            y_positions = y_forward if band_index % 2 == 1 else y_reverse
            for frame_index, y_target in enumerate(y_positions, start=1):
                path.append(
                    ScanPoint(
                        index=index,
                        band=band_index,
                        frame=frame_index,
                        targets={
                            TURNTABLE_AXIS: y_target,
                            ROTOR_AXIS: z_target,
                        },
                    )
                )
                index += 1
        return path

    def _move_to_scan_point(self, targets: dict[str, float], speed: int) -> dict[str, float]:
        moved: dict[str, float] = {}
        for axis in (ROTOR_AXIS, TURNTABLE_AXIS):
            state = self.axis_state[axis]
            target = targets[axis]
            steps = round((target - state.position) * STEPS_PER_MM)
            if steps == 0:
                continue
            if not state.target_allowed(steps):
                raise RuntimeError(f"{axis} soft stop")
            status = self.motor.move(axis, steps, speed, hold=True) if self.motor else {}
            self._store_positions(status)
            moved.update(status)
            self.ui_queue.put(("positions", status))
        if not moved and self.motor:
            moved = self.motor.status()
            self._store_positions(moved)
            self.ui_queue.put(("positions", moved))
        return moved

    def start_scan(self) -> None:
        if self.scan_stop.is_set():
            self.scan_stop.clear()
        if not self.motor:
            self._set_status("Motor server is not connected")
            return
        if self.latest_jpeg is None:
            self._set_status("No camera frame available yet")
            return
        if not self._configure_scan_from_limits():
            self._set_status(f"Scan blocked: {TURNTABLE_AXIS}/{ROTOR_AXIS} limits are missing or current position is outside them")
            return
        self._save_machine_state()
        if not self.move_lock.acquire(blocking=False):
            self._set_status("Move already in progress")
            return
        self._set_motion_controls(False)
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self) -> None:
        try:
            frames = int(self.scan_frames_var.get())
            bands = int(self.scan_bands_var.get())
            speed = int(self.speed_var.get())
            settle = float(self.scan_settle_var.get())
            path = self._build_scan_path()
            session = ROOT / "captures" / time.strftime("scan_%Y%m%d_%H%M%S")
            session.mkdir(parents=True, exist_ok=True)
            manifest = session / "manifest.csv"
            completed = True
            self._set_status(f"Scan started: {session.name}")
            with manifest.open("w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
                        "index",
                        "filename",
                        "band",
                        "frame",
                        "y_target",
                        "z_target",
                        "y_position",
                        "z_position",
                        "bytes",
                        "status",
                    ]
                )
                for point in path:
                    if self.scan_stop.is_set():
                        completed = False
                        self._set_status("Scan stopped")
                        break
                    try:
                        status = self._move_to_scan_point(point.targets, speed)
                    except RuntimeError as exc:
                        completed = False
                        self._set_status(f"Scan stopped: {exc}")
                        break
                    time.sleep(settle)
                    try:
                        frame = self._capture_scan_frame()
                    except Exception as exc:
                        completed = False
                        self._set_status(f"Scan stopped: snapshot failed: {exc}")
                        break
                    filename = f"frame_{point.index:04d}.jpg"
                    (session / filename).write_bytes(frame)
                    status = self.motor.status() if self.motor else status
                    self._store_positions(status)
                    self.ui_queue.put(("positions", status))
                    writer.writerow(
                        [
                            point.index,
                            filename,
                            point.band,
                            point.frame,
                            f"{point.targets[TURNTABLE_AXIS]:.3f}",
                            f"{point.targets[ROTOR_AXIS]:.3f}",
                            f"{status.get(TURNTABLE_AXIS, 0.0):.3f}",
                            f"{status.get(ROTOR_AXIS, 0.0):.3f}",
                            len(frame),
                            status,
                        ]
                    )
                    self._set_status(f"Scan frame {point.index}/{frames * bands} band {point.band}/{bands}")
            if completed:
                self._set_status(f"Scan done: {session.name}")
        except Exception as exc:
            self._set_status(f"Scan failed: {exc}")
        finally:
            self.scan_stop.clear()
            self.move_lock.release()
            self.ui_queue.put(("motion_controls", True))

    def close(self) -> None:
        self.running = False
        self.scan_stop.set()
        if self.motor:
            self.motor.disable()
            self.motor.close()
        try:
            run_ssh(
                "pkill -f '^python3 .*/openscan_motor_server.py' || true; "
                "pkill -f '^python3 .*/openscan_mjpeg_stream.py' || true",
                timeout=5,
            )
        except Exception:
            pass
        stop_master()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
