"""Bounded observer subprocess lifetime, including user cancellation."""
import os
import signal
import subprocess
import time


def communicate(proc, payload, timeout, cancel=None):
    deadline=time.monotonic()+timeout
    first=True
    while True:
        if (cancel is not None and cancel.is_set()) or time.monotonic()>=deadline:
            reason="Observer cancelled" if cancel is not None and cancel.is_set() else "Observer timeout"
            try:os.killpg(proc.pid,signal.SIGTERM)
            except ProcessLookupError:pass
            try:proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid,signal.SIGKILL);proc.wait()
            return reason+"; no automatic retry"
        try:
            proc.communicate(payload if first else None,timeout=min(.5,max(.01,deadline-time.monotonic())))
            return None
        except subprocess.TimeoutExpired:
            first=False
