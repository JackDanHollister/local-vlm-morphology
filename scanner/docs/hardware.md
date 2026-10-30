# Scanner operation

The saved-scan launcher [examples/scans/run_demo.py](../../examples/scans/run_demo.py) never permits live capture. Scanner control is a
separate Linux workflow requiring the physical scanner, SSH access to its Pi,
camera/serial dependencies on that Pi, and an operator beside the machine.

The included implementation targets the original OpenScan Mini installation:
an IMX219 camera, Raspberry Pi, Uno-compatible controller and Y/Z steppers. Its
hardware identity checks and SSH defaults are specific to that installation.
`specimen_lab/hardware.py` defines its host access; `specimen_lab/pi_worker.py`
checks Pi identity and serial source. Credentials must be provisioned separately.
The original manual GUI also requires Tkinter and Pillow; direct serial use needs
PySerial. Remote Picamera2/libcamera and PySerial are Pi system dependencies.

The publication copy uses example connection details and `CONFIGURE_...` markers
in place of the original device identifiers. Before live use, set your Pi host,
SSH identity, remote directory, expected MAC/CPU serial and Uno serial-device
path in the scanner configuration and controller code. Keep the identity checks
enabled and verify them against your own hardware. The saved-results viewer
does not need these settings.

`config/scanner_calibration.json` and `config/specimen_40_photo_plan.json` preserve
the operator's demo calibration. They are historical evidence and are not a new
machine's physical reference. A reset, power loss, stall or manual move can
invalidate the mapping. The saved home bundles must not be used as a live counter
reference without physical calibration and current-state checks.

The measured mapping used one Y turn = 200 steps and Z viewing landmarks
[-9.6875, 2.5625]. Photographs use five bands within those landmarks, with 10-step
endpoint margins, and eight Y orientations per band. These are viewing limits,
not measured mechanical endstops. Live moves are split into checked 10/25/50-step
commands; the driver disables after each command. A software Stop prevents the
next command but cannot interrupt a short MOVE already sent to the firmware.

After installation-specific access and attended calibration are established, the
low-level viewer is started against a separate live evidence directory:

```bash
python3 -m specimen_lab --store experiments/live serve
```

Opening the page alone does not move the scanner. Starting the old Tk GUI does
deploy/restart remote services and opens the serial port, so it must not run at
the same time as automatic acquisition. Do not use it as a connection-only test.
The persistent acquisition worker holds an exclusive hardware lock and stops on
uncertain acknowledgements; it never automatically retries physical movement.

The complete image/ROI set is saved before model observation. Freshness checks
require exposure to start after the final movement and 0.5s settling. The worker
closes and acknowledges disabled motors before analysis begins. Camera/serial
connections stay open only during acquisition; the motor pulse rate is unchanged.

See [firmware protocol](../FIRMWARE_PROTOCOL.md) for controller commands. No live
capture, firmware upload or hardware test is part of the home setup instructions.
