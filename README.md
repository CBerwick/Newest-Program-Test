# Torque Sensor Calibration and Control Suite

This repository contains Python tools for calibrating a torque sensor and operating a Mint E150 motor via a Measurement Computing/Omega DAQ.  It provides a command-line calibration routine and a Tkinter GUI for real‑time monitoring and motor control.

## Features
- Hardware-timed, buffered acquisition driven by the DAQ’s pacer clock
- Automated calibration with an in‑place countdown timer
- GUI displaying voltage or force with optional PID‑style speed feedback
- Motor jog controls and safety limit checking
- Export of logged data to Excel (and CSV if needed)
- Rolling‑average smoothing for stable measurements

## Hardware
- Omega OMB‑DAQ‑2408 (MCC USB‑2408) up to 16 SE / 8 DIFF analog inputs, ±10 V default range
- Mint E150 motor controller connected over USB
- Torque or load cell sensor with a known voltage‑to‑force curve

## Configuration Notes
- Maximum aggregate rate is 1 kS/s per channel; ensure ``fs * channels ≤ 1000``.
- ``DaqStream`` defaults to ±10 V single-ended mode.  Configure ranges or differential inputs by editing ``DaqConfig`` in code.

## Installation
1. Install Python 3.7 or newer
2. Install dependencies:
```bash
pip install mcculw pandas openpyxl pywin32
```

## Quick Start
### Calibration
Collect sensor readings for a fixed duration and save them to Excel using a hardware-paced scan:
```bash
python pymint_torque_calibration.py
```

### GUI Operation
Launch the updated Tkinter interface with MPC-based control by running:
```bash
python shear_test_app.py
```
This application supersedes previous GUI scripts. Use the mode switch to toggle between Simplified and Developer views and adjust the conversion formula as needed for your sensor.

### Jitter Test
Run a background scan for ~60 s and print timestamp jitter statistics:
```bash
python jitter_test.py --fs 100 --duration 60
```

## Tests
Run the test suite with:
```bash
pytest
```

## File Overview
| File | Purpose |
|------|---------|
| `pymint_torque_calibration.py` | Logs voltage data using a hardware-paced scan |
| `shear_test_app.py` | Tkinter GUI with embedded graph for sensor monitoring and motor control |
| `jitter_test.py` | Simple CLI to validate sampling determinism |
| `tests/` | Pytest unit tests |

---
**Author:** Collin Berwick  
**Business:** CBerwick@riteks.com  
**Personal:** CollinBerwick@Gmail.com
