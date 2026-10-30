#!/usr/bin/env python3
"""Send one command to the OpenScan Arduino Uno through Pi_B.

This runs a small standard-library Python snippet over SSH on Pi_B, opens the
Uno serial port, sends a single line, prints the firmware response, and
optionally prints STATUS afterwards.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import textwrap


REMOTE_CODE = r"""
import os
import select
import sys
import termios
import time

port = sys.argv[1]
baud = int(sys.argv[2])
boot_delay = float(sys.argv[3])
read_timeout = float(sys.argv[4])
status_after = sys.argv[5] == "1"
commands = sys.argv[6:]

baud_map = {
    9600: termios.B9600,
    19200: termios.B19200,
    38400: termios.B38400,
    57600: termios.B57600,
    115200: termios.B115200,
}


def read_for(fd, seconds):
    end = time.time() + seconds
    data = b""
    while time.time() < end:
        readable, _, _ = select.select([fd], [], [], 0.05)
        if readable:
            try:
                data += os.read(fd, 4096)
            except BlockingIOError:
                pass
    return data.decode(errors="replace")


fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
try:
    attrs = termios.tcgetattr(fd)
    attrs[4] = baud_map[baud]
    attrs[5] = baud_map[baud]
    attrs[0] = 0
    attrs[1] = 0
    attrs[2] = termios.CS8 | termios.CREAD | termios.CLOCAL
    attrs[3] = 0
    termios.tcsetattr(fd, termios.TCSANOW, attrs)

    time.sleep(boot_delay)
    read_for(fd, 0.25)

    for command in commands:
        os.write(fd, (command + "\n").encode())
        print(read_for(fd, read_timeout), end="")

    if status_after:
        os.write(fd, b"STATUS\n")
        print(read_for(fd, 1.0), end="")
finally:
    os.close(fd)
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", nargs="*", help="Serial command, for example: STATUS or MOVE X 100")
    parser.add_argument("--raw", action="append", help="Additional raw command; can be repeated")
    parser.add_argument("--host", default=os.environ.get("OPENSCAN_PI_HOST", "192.0.2.10"))
    parser.add_argument("--user", default=os.environ.get("OPENSCAN_PI_USER", "scanner"))
    parser.add_argument("--ssh-key", default=os.path.expanduser(os.environ.get("OPENSCAN_SSH_KEY", "~/.ssh/openscan_key")))
    parser.add_argument(
        "--port",
        default=os.environ.get(
            "OPENSCAN_UNO_PORT",
            "/dev/serial/by-id/CONFIGURE_SCANNER_DEVICE",
        ),
    )
    parser.add_argument("--baud", type=int, default=int(os.environ.get("OPENSCAN_UNO_BAUD", "115200")))
    parser.add_argument("--boot-delay", type=float, default=2.2)
    parser.add_argument("--read-timeout", type=float, default=2.0)
    parser.add_argument("--no-status-after", action="store_true")
    args = parser.parse_args()

    commands = []
    if args.command:
        commands.append(" ".join(args.command))
    commands.extend(args.raw or [])
    if not commands:
        parser.error("provide a command or at least one --raw command")

    remote_cmd = shlex.join(
        [
            "python3",
            "-c",
            textwrap.dedent(REMOTE_CODE),
            args.port,
            str(args.baud),
            str(args.boot_delay),
            str(args.read_timeout),
            "0" if args.no_status_after else "1",
        ]
        + commands
    )
    ssh_cmd = [
        "ssh",
        "-i",
        args.ssh_key,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=5",
        f"{args.user}@{args.host}",
        remote_cmd,
    ]
    return subprocess.run(ssh_cmd, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
