#!/usr/bin/env python3
"""MJPEG stream server for the Pi camera on Pi_B."""

from __future__ import annotations

import argparse
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


frame_lock = threading.Lock()
latest_frame: bytes = b""
stream_proc_lock = threading.Lock()
stream_proc: subprocess.Popen | None = None
pause_stream = threading.Event()

still_width = 3280
still_height = 2464
still_quality = 95
still_timeout = 2000


def stop_stream_process() -> None:
    global stream_proc
    with stream_proc_lock:
        proc = stream_proc
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)


def wait_for_stream_idle(timeout: float = 3.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with stream_proc_lock:
            active = stream_proc is not None and stream_proc.poll() is None
        if not active:
            return
        time.sleep(0.05)
    raise RuntimeError("preview stream did not release camera")


def capture_still() -> bytes:
    pause_stream.set()
    stop_stream_process()
    wait_for_stream_idle()
    cmd = [
        "rpicam-still",
        "-n",
        "--width",
        str(still_width),
        "--height",
        str(still_height),
        "--quality",
        str(still_quality),
        "--timeout",
        str(still_timeout),
        "-o",
        "-",
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=20)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace").strip())
        data = result.stdout
        if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            raise RuntimeError("still capture did not return a JPEG")
        return data
    finally:
        pause_stream.clear()


def capture_loop(width: int, height: int, fps: int) -> None:
    global latest_frame, stream_proc
    cmd = [
        "rpicam-vid",
        "--codec",
        "mjpeg",
        "--width",
        str(width),
        "--height",
        str(height),
        "--framerate",
        str(fps),
        "--timeout",
        "0",
        "-o",
        "-",
        "--nopreview",
    ]
    while True:
        if pause_stream.is_set():
            time.sleep(0.05)
            continue
        print("starting rpicam-vid", flush=True)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        with stream_proc_lock:
            stream_proc = proc
        buf = b""
        try:
            assert proc.stdout is not None
            while True:
                if pause_stream.is_set():
                    break
                chunk = proc.stdout.read(4096)
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
                    with frame_lock:
                        latest_frame = frame
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            with stream_proc_lock:
                if stream_proc is proc:
                    stream_proc = None
        time.sleep(1)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *args):
        return

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b'<img src="/stream"><p><a href="/snapshot">snapshot</a></p>')
            return
        if self.path == "/snapshot":
            try:
                frame = capture_still()
            except Exception as exc:
                self.send_response(500)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(str(exc).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(frame)))
            self.end_headers()
            self.wfile.write(frame)
            return
        if self.path != "/stream":
            self.send_response(404)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.end_headers()
        try:
            while True:
                with frame_lock:
                    frame = latest_frame
                if frame:
                    self.wfile.write(b"--frame\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                else:
                    time.sleep(0.05)
        except (BrokenPipeError, ConnectionResetError):
            return


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--width", type=int, default=960)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=8)
    parser.add_argument("--still-width", type=int, default=3280)
    parser.add_argument("--still-height", type=int, default=2464)
    parser.add_argument("--still-quality", type=int, default=95)
    parser.add_argument("--still-timeout", type=int, default=2000)
    args = parser.parse_args()

    global still_width, still_height, still_quality, still_timeout
    still_width = args.still_width
    still_height = args.still_height
    still_quality = args.still_quality
    still_timeout = args.still_timeout

    threading.Thread(target=capture_loop, args=(args.width, args.height, args.fps), daemon=True).start()
    print(f"streaming on :{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
