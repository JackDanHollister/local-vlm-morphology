#!/usr/bin/env python3
"""TCP bridge from the workstation GUI to the OpenScan Arduino firmware.

Run on Pi_B. It keeps the Arduino serial port open so the firmware does not
reset for every jog command.
"""

from __future__ import annotations

import argparse
import os
import select
import socket
import threading
import time
import termios


DEFAULT_SERIAL = "/dev/serial/by-id/CONFIGURE_SCANNER_DEVICE"
AXES = {"X", "Y", "Z", "A"}


class SerialLink:
    def __init__(self, port: str, baud: int, default_speed: int) -> None:
        self.port = port
        self.baud = baud
        self.default_speed = default_speed
        self._lock = threading.RLock()
        self.enabled = False
        self.speed_us: int | None = None
        self._fd = self._open()
        self.banner = self._read_for(2.8)
        self.send("DISABLE", timeout=1.0)

    def _open(self) -> int:
        baud_map = {
            9600: termios.B9600,
            19200: termios.B19200,
            38400: termios.B38400,
            57600: termios.B57600,
            115200: termios.B115200,
        }
        fd = os.open(self.port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        attrs = termios.tcgetattr(fd)
        attrs[4] = baud_map[self.baud]
        attrs[5] = baud_map[self.baud]
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        return fd

    def _read_for(self, seconds: float) -> list[str]:
        end = time.time() + seconds
        data = b""
        while time.time() < end:
            readable, _, _ = select.select([self._fd], [], [], 0.05)
            if readable:
                try:
                    chunk = os.read(self._fd, 4096)
                except BlockingIOError:
                    chunk = b""
                if not chunk:
                    break
                data += chunk
        return [line.strip() for line in data.decode(errors="replace").splitlines() if line.strip()]

    def _read_response(self, timeout: float) -> list[str]:
        end = time.time() + timeout
        data = b""
        saw_line_at: float | None = None
        while time.time() < end:
            readable, _, _ = select.select([self._fd], [], [], 0.02)
            if readable:
                try:
                    chunk = os.read(self._fd, 4096)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    data += chunk
                    saw_line_at = time.time()
                else:
                    break
            elif saw_line_at is not None and time.time() - saw_line_at > 0.03:
                break
        return [line.strip() for line in data.decode(errors="replace").splitlines() if line.strip()]

    def _drain_pending(self) -> None:
        while True:
            readable, _, _ = select.select([self._fd], [], [], 0)
            if not readable:
                return
            try:
                chunk = os.read(self._fd, 4096)
            except BlockingIOError:
                return
            if not chunk:
                return

    def send(self, command: str, timeout: float = 2.0) -> list[str]:
        with self._lock:
            self._drain_pending()
            os.write(self._fd, (command + "\n").encode())
            return self._read_response(timeout)

    def status(self) -> list[str]:
        return self.send("STATUS", timeout=1.0)

    def set_limits(self, axis: str, min_mm: float, max_mm: float) -> list[str]:
        return self.send(f"SET_LIMITS {axis} {min_mm:g} {max_mm:g}", timeout=1.0)

    def zero(self, axis: str) -> list[str]:
        return self.send(f"ZERO {axis}", timeout=1.0)

    def move(self, axis: str, steps: int, speed: int | None = None, disable: bool = True) -> list[str]:
        with self._lock:
            delay = int(speed or self.default_speed)
            move_timeout = max(2.0, abs(int(steps)) * delay / 1_000_000.0 + 1.5)
            lines: list[str] = []
            if not self.enabled:
                lines.extend(self.send("ENABLE", timeout=1.0))
                self.enabled = True
                time.sleep(0.02)
            if self.speed_us != delay:
                lines.extend(self.send(f"SPEED {delay}", timeout=1.0))
                self.speed_us = delay
            lines.extend(self.send(f"MOVE {axis} {int(steps)}", timeout=move_timeout))
            lines.extend(self.status())
            if disable:
                time.sleep(0.08)
                lines.extend(self.send("DISABLE", timeout=1.0))
                self.enabled = False
            return lines

    def disable(self) -> list[str]:
        lines = self.send("DISABLE", timeout=1.0)
        self.enabled = False
        return lines

    def close(self) -> None:
        try:
            self.disable()
        except Exception:
            pass
        os.close(self._fd)


def validate_axis(axis: str) -> str:
    axis = axis.upper()
    if axis not in AXES:
        raise ValueError(f"bad axis: {axis}")
    return axis


def handle_client(conn: socket.socket, link: SerialLink, addr: tuple[str, int]) -> None:
    with conn:
        conn.settimeout(0.5)
        buf = ""
        while True:
            try:
                data = conn.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            buf += data.decode(errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                if not line.lower().startswith("status"):
                    print(f"cmd {addr}: {line}", flush=True)
                try:
                    parts = line.split()
                    cmd = parts[0].lower()
                    if cmd == "quit":
                        conn.sendall(b"OK QUIT\nEND\n")
                        return
                    if cmd == "status":
                        lines = link.status()
                    elif cmd == "disable":
                        lines = link.disable()
                    elif cmd == "zero" and len(parts) == 2:
                        lines = link.zero(validate_axis(parts[1]))
                    elif cmd == "limits" and len(parts) == 4:
                        lines = link.set_limits(validate_axis(parts[1]), float(parts[2]), float(parts[3]))
                    elif cmd == "move" and len(parts) in (3, 4, 5):
                        axis = validate_axis(parts[1])
                        steps = int(parts[2])
                        speed = None
                        hold = False
                        for extra in parts[3:]:
                            if extra.lower() in {"hold", "true", "1", "on"}:
                                hold = True
                            else:
                                speed = int(extra)
                        lines = link.move(axis, steps, speed=speed, disable=not hold)
                    elif cmd == "raw" and len(parts) > 1:
                        raw = line.split(" ", 1)[1]
                        timeout = 2.0
                        if raw.upper().startswith("MOVE "):
                            timeout = 20.0
                        lines = link.send(raw, timeout=timeout)
                    else:
                        lines = [f"ERR COMMAND {line}"]
                except Exception as exc:
                    lines = [f"ERR {exc}"]
                conn.sendall(("\n".join(lines) + "\nEND\n").encode())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10002)
    parser.add_argument("--serial", default=DEFAULT_SERIAL)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--speed", type=int, default=6000)
    args = parser.parse_args()

    link = SerialLink(args.serial, args.baud, args.speed)
    print("OpenScan motor server ready", flush=True)
    for line in link.banner:
        print(f"banner: {line}", flush=True)

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.host, args.port))
    srv.listen(4)
    try:
        while True:
            conn, addr = srv.accept()
            print(f"client {addr}", flush=True)
            threading.Thread(target=handle_client, args=(conn, link, addr), daemon=True).start()
    finally:
        srv.close()
        link.close()


if __name__ == "__main__":
    raise SystemExit(main())
