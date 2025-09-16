# Shear Test Control and Data Acquisition Toolkit

## Overview
This repository contains the scripts that drive our shear test stand.  The
core application is a Tkinter GUI that streams torque sensor data through a
Measurement Computing DAQ while commanding a Mint E150 motor controller.  The
suite also includes a command-line calibration routine, a jitter diagnostic, and
an S–N fatigue analysis helper.

All tooling is written in Python and targets Windows where the MCC Universal
Library (`mcculw`) and Mint COM drivers are available.  The analysis utilities
and tests can run on other platforms using the provided stubs.

## Components
### `shear_test_app.py`
Tkinter application used during shear testing.  Key features include:

- **Simplified / Developer modes** – switch with the radio buttons or by
  pressing <kbd>F12</kbd>.  Simplified mode exposes only the controls needed on
  the production floor; Developer mode enables tuning panels, the live plot, and
  model management.
- **Hardware-paced acquisition** – samples the configured analog inputs through
  `DaqStream` so the DAQ owns the timing.  Data are buffered in memory for the
  graph and mirrored to a rotating SQLite database under `logs/` for recovery.
- **Motor control** – connects to the Mint E150 controller over COM,
  exposes Enable/Jog/Stop actions, enforces slew and safety limits, and supports
  PID-style jog trimming.
- **Model predictive control (MPC)** – ships with an ARX plant model and an MPC
  regulator.  Users can enable cyclic A↔B operation with tolerance bands, ramp
  to a target force over a specified time, and identify new ARX parameters from
  collected data.
- **Data export** – save the in-memory run history to Excel, including the live
  force/voltage traces and calculated force values.

All persistent data are written to `logs/` (an empty placeholder file is kept in
version control so the folder exists before the first run).

### `pymint_torque_calibration.py`
Interactive calibration helper that reads the torque sensor through the DAQ for
a fixed duration, converts each sample to pounds-force using the same
calibration as the GUI, and writes the results to Excel.  Metadata prompts at
startup are used to build the output filename.

### `jitter_test.py`
Command-line utility to validate deterministic hardware-paced sampling.  It
streams one or more analog channels for a requested duration, prints summary
statistics about the sample-to-sample spacing, and saves the captured data as a
CSV file.

### `daq_stream.py`
Backend helper that wraps the MCC Universal Library (or `uldaq` when available)
with a worker thread and timestamped queues.  Both the GUI and the CLI tools use
this class to avoid manual buffer management when performing continuous scans.

### `Analysis/sn_tool.py`
Rainflow-based fatigue post-processing and S–N curve fitting.  It accepts Excel
or CSV inputs, performs hysteresis filtering, applies optional mean-stress
corrections, bins the cycles, and can export histograms and fitted parameters.
Refer to `Analysis/README_sn_tool.md` for detailed usage examples and
command-line flags.

## Installation
1. Install Python 3.9 or newer on Windows.
2. Install the required Python packages:
   ```bash
   pip install mcculw numpy pandas matplotlib
   ```
3. Install optional packages if you need the extended functionality:
   - `osqp` – enables constrained MPC optimisation in the GUI.
   - `control` – prints transfer functions for quick debugging inside the GUI.
   - `scipy` – unlocks Welch/Dirlik PSD damage calculations in `sn_tool.py`.

Mint controller access also requires the Mint COM drivers (`win32com`) provided
with the hardware.  The tests run using the lightweight stubs bundled with the
repository, so you can exercise the code without the hardware attached.

## Usage
### Launch the GUI
```bash
python shear_test_app.py
```
Follow the prompts in the application to start the DAQ, enable the motor, and
choose between Simplified and Developer modes.  Log databases are saved under
`logs/` and can be opened directly from the Developer tab.

### Run a calibration capture
```bash
python pymint_torque_calibration.py
```
Provide the project/test identifiers when prompted.  The script streams the
configured channel for the default two-minute duration and writes an Excel file
containing time and load columns.

### Measure jitter
```bash
python jitter_test.py --fs 100 --duration 60 --channels 0 1
```
The command above records two channels at 100 Hz for one minute, prints the
sample interval statistics, and writes `jitter_test.csv` in the working
directory.

### Fatigue processing
```bash
python Analysis/sn_tool.py test1.xlsx --time-col "Time (s)" --signal-col "Force (lbf)" \
  --hysteresis-threshold 2.0 --goodman-Su 60000 --export-cycles --save-prefix run1
```
See `Analysis/README_sn_tool.md` for additional examples covering S–N fitting
and PSD-based damage rates.

## Testing
Install `pytest` and run the suite from the project root:
```bash
pytest
```
The tests rely on stub modules for the DAQ, Tkinter, and COM layers so they can
execute without hardware.

## Repository layout
```
Analysis/                Fatigue S–N processing tool and documentation
logs/                    Placeholder for SQLite run logs generated by the GUI
daq_stream.py            Shared data acquisition helper
jitter_test.py           Hardware-paced sampling jitter diagnostic
pymint_torque_calibration.py  Torque sensor calibration capture script
shear_test_app.py        Tkinter GUI for shear testing
tests/                   Automated tests and fixtures
```
