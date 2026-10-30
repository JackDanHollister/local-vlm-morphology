# Firmware Serial Protocol

Baud:

```text
115200
```

Line ending:

```text
\n
```

Startup banner:

```text
N3D_READY
```

## Commands

### Enable Drivers

```text
ENABLE
```

Response:

```text
OK ENABLE
```

Sets CNC shield enable pin D8 LOW. On common shields this enables all stepper drivers.

### Disable Drivers

```text
DISABLE
```

Response:

```text
OK DISABLE
```

Sets D8 HIGH.

### Set Speed

```text
SPEED 2000
```

Response:

```text
OK SPEED
```

This sets delay between step pulses in microseconds. Lower is faster. During bring-up, `2000` was used because it is slow and visible. The GUI now defaults to `6000` while movement is still being tuned because it is more conservative.

### Set Ramp

```text
RAMP 50 4000
```

Response:

```text
OK RAMP
```

This is present in the updated firmware source but must be uploaded to the Uno before it affects the scanner. It sets a simple start/stop ramp: first argument is the maximum ramp length in steps, second argument is the extra delay added at the start and end of a move. `RAMP 0 0` disables ramping.

### Move By Steps

```text
MOVE Y 800
MOVE Z -800
```

Response:

```text
OK MOVE
```

The optional fourth parameter overrides speed for that move:

```text
MOVE Y 800 2000
```

### Move By Millimetres

```text
MOVE_MM Y 5
```

Response:

```text
OK MOVE_MM
```

Default scale is 80 steps/mm on all axes. This is not calibrated yet.

### Set Steps Per Millimetre

```text
SET_STEPS_PER_MM Y 80
```

Response:

```text
OK SET_STEPS_PER_MM
```

### Set Soft Limits

```text
SET_LIMITS Y -50 200
```

Response:

```text
OK SET_LIMITS
```

If a requested move exceeds limits, firmware clamps it and returns:

```text
WARN LIMIT
```

### Zero Axis

```text
ZERO Y
```

Response:

```text
OK ZERO
```

### Status

```text
STATUS
```

Response example:

```text
STATUS X=0.000 Y=0.000 Z=0.000 A=0.000
```

Important: firmware position counters reset when the Arduino resets. There are no endstops/homing switches implemented yet.

For physical movement through the CNC shield, use `ENABLE` before moves. The GUI keeps the serial port open through a Pi-side bridge and wraps jog moves in:

```text
ENABLE
SPEED 6000
MOVE <axis> <steps>
STATUS
```

By default the GUI leaves the drivers enabled after each jog. This avoids relaxation/backlash between small moves. Use the GUI's `Disable Drivers` button when the rig should be left idle.

The Pi-side TCP bridge also accepts a host-only hold flag:

```text
move Y 100 6000 hold
```

That flag is not part of the Arduino serial firmware protocol; it only tells the Pi bridge not to send `DISABLE` after the move.
