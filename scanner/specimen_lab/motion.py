"""Shared local/Pi motion policy in firmware units; no hardware side effects."""

import math
import re

POLICY_VERSION = "attended-mixed-steps-v2"
STEPS_PER_UNIT = 80
MAX_TRAVEL_STEPS = 400
DEMO_TRAVEL_STEPS = 2000
VIEWS = {"CURRENT": (None, 0)}
for _axis in ("Y", "Z"):
    for _direction, _sign in (("PLUS", 1), ("MINUS", -1)):
        for _size in (10, 25, 50):
            VIEWS[f"{_axis}_{_direction}_{_size}_STEPS"] = (_axis, _sign * _size)


def validate_limits(limits):
    if not isinstance(limits, dict) or set(limits) != {"Y", "Z"}:
        raise ValueError("Both saved Y/Z limits are required")
    for axis, bounds in limits.items():
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            raise ValueError("Expected [min,max] limits")
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in bounds) or bounds[0] >= bounds[1]:
            raise ValueError(f"Invalid saved {axis} limits")
    return limits


def positions(lines):
    statuses = [line for line in lines if line.startswith("STATUS ")]
    if len(statuses) != 1:
        raise ValueError("Expected one firmware STATUS response")
    result = {}
    for axis in ("Y", "Z"):
        matches = re.findall(r"\b" + axis + r"=(-?\d+(?:\.\d+)?)\b", statuses[0])
        if len(matches) != 1:
            raise ValueError(f"Missing/ambiguous {axis} position")
        value = float(matches[0])
        # STATUS is rounded to 0.001; motor positions must still lie on the
        # 1/80 firmware-unit step grid. Never use the saved GUI position.
        snapped = round(value * STEPS_PER_UNIT) / STEPS_PER_UNIT
        if abs(value - snapped) > 0.00051:
            raise ValueError("Unexpected firmware step scale")
        result[axis] = snapped
    return result


def check_target(position, axis, steps, limits):
    validate_limits(limits)
    if (axis, steps) not in VIEWS.values() or type(steps) is not int:
        raise ValueError("Movement outside the fixed catalogue")
    if set(position) != {"Y", "Z"}:
        raise ValueError("Both current positions required")
    for key, value in position.items():
        if type(value) not in (int, float) or not math.isfinite(value) or not limits[key][0] <= value <= limits[key][1]:
            raise ValueError(f"Current {key} position outside saved limits")
    target = dict(position)
    if axis:
        target[axis] = (round(position[axis] * STEPS_PER_UNIT) + steps) / STEPS_PER_UNIT
        if not limits[axis][0] <= target[axis] <= limits[axis][1]:
            raise ValueError(f"Requested {axis} target {target[axis]} crosses saved limits {limits[axis]}")
    return target


def travel_budget(value):
    if type(value) is not int or value not in (MAX_TRAVEL_STEPS, DEMO_TRAVEL_STEPS):
        raise ValueError("Unknown movement budget")
    return value


def available_views(position, limits, used_steps=0, max_travel_steps=MAX_TRAVEL_STEPS):
    travel_budget(max_travel_steps)
    result = []
    for name, (axis, steps) in VIEWS.items():
        if axis is None or used_steps + abs(steps) > max_travel_steps:
            continue
        try:
            check_target(position, axis, steps, limits)
        except ValueError:
            continue
        result.append(name)
    return result
