#!/usr/bin/env python3
"""Translate the operator-mapped scan into a newly observed bottom/front zero.

This changes files only. It never opens serial hardware or sends a motor command.
The previous calibration and plan are embedded in an immutable incident record.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime
from pathlib import Path


PROJECT = Path(__file__).resolve().parents[1]
CONFIG = PROJECT / "config"
STORE = PROJECT / "experiments" / "specimen-lab"


def canonical(value):
    return json.dumps(value, indent=2, sort_keys=False) + "\n"


def snapped(value):
    return round(value * 80) / 80


def translated_position(position, y_offset, z_offset):
    return {"Y": snapped(position["Y"] + y_offset),
            "Z": snapped(position["Z"] + z_offset)}


def translate_move_plan(previous, y_offset, z_offset, recorded_at):
    plan = json.loads(json.dumps(previous))
    plan.update(schema="scanner-40-photo-plan-v3", recorded_at=recorded_at,
                Z_order="bottom-to-overhead",
                start_position={"Y": 0.0, "Z": 0.0})
    old_photos = previous["photos"]
    bands = []
    for band in reversed(range(5)):
        group = old_photos[band * 8:(band + 1) * 8]
        by_phase = {photo["Y_phase_degrees"]: photo for photo in group}
        phases = list(range(0, 360, 45)) if len(bands) % 2 == 0 else list(range(315, -1, -45))
        bands.append([by_phase[float(phase)] for phase in phases])

    photos = []
    current = dict(plan["start_position"])
    total = 0
    for source in [photo for band in bands for photo in band]:
        target = translated_position(source["target"], y_offset, z_offset)
        commands = []
        for axis in ("Z", "Y"):
            steps = round((target[axis] - current[axis]) * 80)
            sign = 1 if steps > 0 else -1
            remaining = abs(steps)
            while remaining:
                size = 50 if remaining >= 50 else 25 if remaining % 25 == 0 else 10
                if size > remaining:
                    raise ValueError("Translated target is outside the fixed movement catalogue")
                commands.append({"axis": axis, "steps": sign * size})
                remaining -= size
                total += size
            current[axis] = target[axis]
        photos.append({"photo": len(photos) + 1,
                       "tilt_band": len(photos) // 8 + 1,
                       "Y_phase_degrees": source["Y_phase_degrees"],
                       "target": target,
                       "transit_commands": commands})
    plan["photos"] = photos
    plan["total_commanded_travel_steps"] = total
    return plan


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", required=True,
                        help="Exact controller STATUS observed while operator confirms bottom/front")
    parser.add_argument("--recorded-at", required=True,
                        help="ISO timestamp from Thor, after the live observation")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    datetime.fromisoformat(args.recorded_at)
    if args.status != "STATUS X=0.000 Y=0.000 Z=0.000 A=0.000":
        raise SystemExit("Refusing rebase: expected the confirmed zeroed bottom/front STATUS")

    cal_path = CONFIG / "scanner_calibration.json"
    plan_path = CONFIG / "specimen_40_photo_plan.json"
    cal_raw, plan_raw = cal_path.read_bytes(), plan_path.read_bytes()
    previous_cal, previous_plan = json.loads(cal_raw), json.loads(plan_raw)
    if previous_cal.get("schema") != "scanner-operator-calibration-v1":
        raise SystemExit("Refusing rebase: unexpected calibration schema")
    if previous_plan.get("schema") != "scanner-40-photo-plan-v1":
        raise SystemExit("Refusing rebase: calibration has already been rebased")

    old_front = previous_cal["counter_frame"]["current_position"]["Y"]
    old_bottom = previous_cal["Z"]["underside_step_index"] / 80
    y_offset, z_offset = -old_front, -old_bottom
    calibration = json.loads(json.dumps(previous_cal))
    calibration.update(recorded_at=args.recorded_at,
        source=("Operator beside scanner confirmed the specimen faces straight forward at the Z bottommost "
                "physical reference; controller STATUS was Y=0/Z=0. Prior measured 200-step Y turn and "
                "980-step Z viewing span were translated without commanding ZERO."),
        raw_status_at_completion=args.status,
        purpose="Working rebased calibration for the attended 40-photo method demonstration")
    calibration["counter_frame"] = {
        "current_position": {"Y": 0.0, "Z": 0.0},
        "reset_or_zero_issued_by_assistant": False,
        "validity": "Re-establish after zero, reset, power loss or motor slip/stall",
        "physical_reference": "operator-confirmed front orientation at bottommost Z position",
    }
    calibration["Y"].update(start_step_index=0, end_step_index=200,
        start_firmware_position=0.0, end_firmware_position=2.5,
        front_facing_phase_step=0)
    calibration["Z"].update(overhead_step_index=-980, underside_step_index=0,
        operator_viewing_range=[-12.25, 0.0], proposed_photo_range=[-12.125, -0.125])
    calibration["qualification"] = (
        "Current bottom/front counter anchor is operator-established; Y turn and Z span are translated from "
        "the 2026-09-11 operator mapping without a new full-range traversal; no metrology accuracy claim")
    calibration["automatic_scan_restarted"] = False
    calibration["prior_calibration_sha256"] = hashlib.sha256(cal_raw).hexdigest()

    plan = translate_move_plan(previous_plan, y_offset, z_offset, args.recorded_at)
    incident = {
        "schema": "scanner-counter-frame-rebase-v1",
        "recorded_at": args.recorded_at,
        "operator_report": "Z is at its bottommost point and the specimen faces directly toward the operator",
        "controller_status": args.status,
        "assistant_zero_or_reset_issued": False,
        "prior_calibration_sha256": hashlib.sha256(cal_raw).hexdigest(),
        "prior_plan_sha256": hashlib.sha256(plan_raw).hexdigest(),
        "prior_calibration": previous_cal,
        "prior_plan": previous_plan,
        "new_calibration": calibration,
        "new_plan": plan,
        "scope": "Coordinate translation only; no motion or camera action",
    }
    print(canonical({"calibration": calibration, "plan": plan,
                     "incident_sha256": hashlib.sha256(canonical(incident).encode()).hexdigest()}), end="")
    if not args.apply:
        return
    stamp = datetime.fromisoformat(args.recorded_at).strftime("%Y%m%d-counter-frame-rebase-%H%M%S.json")
    incident_path = STORE / "incidents" / stamp
    incident_path.parent.mkdir(parents=True, exist_ok=True)
    with incident_path.open("x") as out:
        out.write(canonical(incident))
    cal_path.write_text(canonical(calibration))
    plan_path.write_text(canonical(plan))
    print(f"Applied rebase; incident={incident_path}")


if __name__ == "__main__":
    main()
