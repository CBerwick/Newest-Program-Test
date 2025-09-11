# -*- coding: utf-8 -*-
"""Torque sensor calibration script.

This module reads a single analog input channel using the MCC Universal
Library and converts the voltage to pounds-force based on the same
calibration used in the GUI application.  The script collects data for a
fixed duration while displaying a countdown timer and finally exports the
results to an Excel file.
"""

import math
import time

import pandas as pd
from mcculw import ul
from mcculw.enums import AnalogInputMode, ULRange

from daq_stream import DaqConfig, DaqStream

# ── CONFIGURATION ──────────────────────────────────────────────────────────────
# DAQ board and channel configuration
BOARD_NUM = 0
TORQUE_SENSOR_CHANNEL = 0  # AI0
VOLTAGE_RANGE = ULRange.BIP10VOLTS  # ±10 V full-scale input

# Timing configuration
SAMPLE_INTERVAL = 0.05  # Seconds between readings
DURATION = 2 * 60       # Total calibration time (seconds)

# ── SET THE INPUT MODE (only once) ──────────────────────────────────────────────
# Configure the analog input mode.  If your DAQ channel does not support
# differential measurements you may need to move the sensor to a channel
# that does.
ul.a_input_mode(BOARD_NUM, AnalogInputMode.DIFFERENTIAL)
time.sleep(0.05)   # allow ~50 ms for the mux to settle

def read_torque_voltage():
    """Return a single voltage reading from the torque sensor."""
    raw32 = ul.a_in_32(BOARD_NUM, TORQUE_SENSOR_CHANNEL, VOLTAGE_RANGE)
    volts = ul.to_eng_units_32(BOARD_NUM, VOLTAGE_RANGE, raw32)
    return volts


def volts_to_force_lbf(volts):
    """Return the load in pounds-force corresponding to ``volts``.

    The coefficients mirror those used by the GUI so calibration is
    consistent between the command-line tool and the Tkinter interface.
    """
    mass_kg = 64.0690359 * math.sqrt(volts ** 2) - 0.0152348083
    force_n = mass_kg * 9.81
    return force_n / 4.448


# ── MAIN ROUTINE ─────────────────────────────────────────────────────────────

def main():
    """Run the interactive calibration routine."""

    # ------------------------------------------------------------------
    # Gather metadata for the output filename
    # ------------------------------------------------------------------

    project = input("Project name: ")
    test_id = input("Test ID: ")
    iteration = input("Iteration: ")

    base_name = f"{project}_{test_id}_{iteration}".replace(" ", "_")
    excel_path = f"{base_name}.xlsx"

    # Storage for time stamps and converted force readings
    timestamps: list[float] = []
    loads: list[float] = []

    stream = DaqStream(BOARD_NUM)
    cfg = DaqConfig(channels=[TORQUE_SENSOR_CHANNEL], fs=1.0 / SAMPLE_INTERVAL, ul_range=VOLTAGE_RANGE)
    stream.start(cfg)
    start_time = stream.t0
    end_time = start_time + DURATION
    last_printed_second = None

    while time.monotonic() < end_time:
        block = stream.read()
        if block is None:
            time.sleep(0.01)
            continue
        ts, data = block
        for t, v in zip(ts, data[:, 0]):
            elapsed = t - start_time
            remaining_sec = int(end_time - t) if end_time > t else 0
            if remaining_sec != last_printed_second:
                print(f"\rTime remaining: {remaining_sec:2d} ", end="", flush=True)
                last_printed_second = remaining_sec
            timestamps.append(elapsed)
            loads.append(volts_to_force_lbf(v))

    print()
    stream.stop()

    # Build a DataFrame and export to Excel
    df = pd.DataFrame({
        "Time (s)": timestamps,
        "Load (lbf)": loads,
    })

    df.to_excel(excel_path, index=False)

    print(f"Calibration data saved to:\n • {excel_path}")


if __name__ == "__main__":  # pragma: no cover - manual execution only
    main()
