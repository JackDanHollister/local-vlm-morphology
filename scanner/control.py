#!/usr/bin/env python3
"""
Simple serial controller for N3D Scanner Arduino firmware.
"""
from __future__ import annotations

import argparse
import sys
import time

import serial


def open_serial(port: str, baud: int, dtr: str, rts: str) -> serial.Serial:
    ser = serial.Serial()
    ser.port = port
    ser.baudrate = baud
    ser.timeout = 2
    if dtr != "unchanged":
        ser.dtr = dtr == "on"
    if rts != "unchanged":
        ser.rts = rts == "on"
    ser.open()
    time.sleep(2)
    return ser


def read_ready(ser: serial.Serial, timeout_s: float = 3.0) -> bool:
    start = time.time()
    while time.time() - start < timeout_s:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if line:
            if "N3D_READY" in line:
                print(line)
                return True
    print("Warning: did not see N3D_READY banner")
    return False


def send_cmd(
    ser: serial.Serial,
    cmd: str,
    response_window_s: float = 2.0,
    line_ending: str = "\n",
) -> None:
    ser.write(f"{cmd}{line_ending}".encode("utf-8"))
    ser.flush()

    start = time.time()
    saw_response = False
    while time.time() - start < response_window_s:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if line:
            print(line)
            saw_response = True

    if not saw_response:
        print(f"Warning: no response to {cmd}")


def main() -> int:
    parser = argparse.ArgumentParser(description="N3D Scanner stepper control")
    parser.add_argument("--port", required=True, help="Serial port, e.g. /dev/tty.usbmodemXXXX")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--speed", type=int, help="Step delay in microseconds (lower is faster)")
    parser.add_argument("--enable", action="store_true", help="Enable drivers")
    parser.add_argument("--disable", action="store_true", help="Disable drivers")
    parser.add_argument("--axis", choices=["X", "Y", "Z", "A"], help="Target axis")
    parser.add_argument("--move-steps", type=int, help="Move axis by steps (+/-)")
    parser.add_argument("--move-mm", type=float, help="Move axis by mm (+/-)")
    parser.add_argument("--set-steps-per-mm", type=float, help="Set steps per mm for axis")
    parser.add_argument(
        "--set-limits",
        nargs=2,
        type=float,
        metavar=("MIN_MM", "MAX_MM"),
        help="Set soft limits for axis in mm",
    )
    parser.add_argument("--zero", action="store_true", help="Zero axis position")
    parser.add_argument("--status", action="store_true", help="Print axis status")
    parser.add_argument("--raw", action="append", help="Send a raw command line; can be repeated")
    parser.add_argument(
        "--response-window",
        type=float,
        default=2.0,
        help="Seconds to collect serial responses after each command",
    )
    parser.add_argument(
        "--line-ending",
        choices=["lf", "crlf", "cr"],
        default="lf",
        help="Line ending to send after each command",
    )
    parser.add_argument("--dtr", choices=["on", "off", "unchanged"], default="unchanged")
    parser.add_argument("--rts", choices=["on", "off", "unchanged"], default="unchanged")
    parser.add_argument(
        "--post-ready-delay",
        type=float,
        default=0.0,
        help="Seconds to wait after the ready banner before sending commands",
    )
    args = parser.parse_args()

    axis_required = any(
        [
            args.move_steps is not None,
            args.move_mm is not None,
            args.set_steps_per_mm is not None,
            args.set_limits is not None,
            args.zero,
        ]
    )
    if axis_required and not args.axis:
        print("Error: --axis is required for axis-specific commands.")
        return 2

    ser = open_serial(args.port, args.baud, args.dtr, args.rts)
    try:
        read_ready(ser)
        if args.post_ready_delay > 0:
            time.sleep(args.post_ready_delay)
        endings = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}
        line_ending = endings[args.line_ending]

        if args.enable:
            send_cmd(ser, "ENABLE", args.response_window, line_ending)

        if args.speed:
            send_cmd(ser, f"SPEED {args.speed}", args.response_window, line_ending)

        if args.set_steps_per_mm is not None:
            send_cmd(
                ser,
                f"SET_STEPS_PER_MM {args.axis} {args.set_steps_per_mm}",
                args.response_window,
                line_ending,
            )

        if args.set_limits is not None:
            min_mm, max_mm = args.set_limits
            send_cmd(ser, f"SET_LIMITS {args.axis} {min_mm} {max_mm}", args.response_window, line_ending)

        if args.zero:
            send_cmd(ser, f"ZERO {args.axis}", args.response_window, line_ending)

        if args.move_steps is not None:
            send_cmd(ser, f"MOVE {args.axis} {args.move_steps}", args.response_window, line_ending)

        if args.move_mm is not None:
            send_cmd(ser, f"MOVE_MM {args.axis} {args.move_mm}", args.response_window, line_ending)

        if args.status:
            send_cmd(ser, "STATUS", args.response_window, line_ending)

        for raw_cmd in args.raw or []:
            send_cmd(ser, raw_cmd, args.response_window, line_ending)

        if args.disable:
            send_cmd(ser, "DISABLE", args.response_window, line_ending)

    finally:
        ser.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())
