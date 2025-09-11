# -*- coding: utf-8 -*-
"""Tkinter-based interface for DAQ monitoring and motor control.

The application continuously reads an analog input channel, displays the
measured value in various units and optionally drives a Mint E150 motor
controller for jog and torque-feedback modes.
"""

import math
import threading
import time

import tkinter as tk
from tkinter import ttk

from mcculw import ul
from mcculw.enums import ULRange
import win32com.client

try:
    import pythoncom
except ImportError:  # pragma: no cover - fallback for non-Windows test envs
    class _DummyPythoncom:
        def CoInitialize(self):
            pass

        def CoUninitialize(self):
            pass

    pythoncom = _DummyPythoncom()

# ── GLOBAL CONSTANTS ────────────────────────────────────────────────────────────
# Motor controller configuration
MOTOR_NODE_ID = 2  # USB node ID for E150 controller
AXIS_ID = 0        # Motor axis index (0-based)

# Conversion helpers for jog speed
DEGREES_PER_STEP = 36.0   # One motor step equals 36 degrees
DEFAULT_JOG_RPM = 100     # Default jog speed presented in the UI

# Simple torque feedback constants (PID controller)
TORQUE_SETPOINT_LBF = 5.0  # Desired torque when feedback is enabled
TORQUE_KP = 0.1            # Proportional gain
TORQUE_KI = 0.05           # Integral gain
TORQUE_KD = 0.01           # Derivative gain
SAFETY_MAX_RPM = 1000      # Stop motor if commanded RPM exceeds this

# DAQ configuration
BOARD_NUM = 0
CHANNEL = 0          # Single analog input channel
VOLTAGE_RANGE = ULRange.BIP10VOLTS  # ±10 V differential range
SAMPLE_INTERVAL = 0.01  # Seconds between each DAQ read
ROLLING_PERIOD = 15     # Number of points in rolling average

# Track current commanded RPM for torque feedback adjustments
current_rpm = DEFAULT_JOG_RPM

# ── DATA LOGGING STORAGE ──────────────────────────────────────────────────────
# These globals are populated while the DAQ is active and are exported when
# stopped.
log_start_time = None
log_time = []
log_voltage = []
log_mass_kg = []
log_force_lbf = []

# ── PID STATE STORAGE ─────────────────────────────────────────────────────────
error_integral = 0.0
last_error = 0.0
last_time = None

# These globals maintain state across PID updates
# ── HELPER FUNCTIONS ───────────────────────────────────────────────────────────
def volts_to_force_lbf(volts):
    """Convert a voltage reading to pounds-force using the GUI's calibration."""
    mass_kg = 64.0690359 * math.sqrt(volts ** 2) - 0.0152348083
    force_n = mass_kg * 9.81
    return force_n / 4.448


def pid_adjust_speed(motor_ctrl, target_rpm, current_rpm, kp=0.1, axis_id=AXIS_ID):
    """Apply a simple proportional controller and update jog speed."""
    error = target_rpm - current_rpm
    new_rpm = current_rpm + kp * error
    # Controller expects steps/sec; 6.0 steps equals 1 RPM
    motor_ctrl.SetJog(axis_id, new_rpm / 6.0)
    return new_rpm

def pid_torque_control(
    motor_ctrl,
    target_lbf,
    current_lbf,
    current_rpm,
    kp=TORQUE_KP,
    ki=TORQUE_KI,
    kd=TORQUE_KD,
    axis_id=AXIS_ID,
):
    """Compute a PID update based on torque feedback and command new RPM."""
    global error_integral, last_error, last_time

    now = time.time()
    if last_time is None:
        dt = SAMPLE_INTERVAL
    else:
        dt = now - last_time
        if dt <= 0:
            dt = SAMPLE_INTERVAL
    last_time = now

    error = target_lbf - current_lbf
    error_integral += error * dt
    derivative = (error - last_error) / dt

    pid_output = (kp * error) + (ki * error_integral) + (kd * derivative)

    new_rpm = current_rpm + pid_output * dt
    if new_rpm > SAFETY_MAX_RPM:
        new_rpm = SAFETY_MAX_RPM
    elif new_rpm < -SAFETY_MAX_RPM:
        new_rpm = -SAFETY_MAX_RPM

    # Controller expects steps/sec; 6.0 steps equals 1 RPM
    motor_ctrl.SetJog(axis_id, new_rpm / 6.0)
    last_error = error
    return new_rpm

def reset_pid():
    """Reset PID state variables."""
    global error_integral, last_error, last_time
    error_integral = 0.0
    last_error = 0.0
    last_time = None

def is_speed_safe(rpm, limit=SAFETY_MAX_RPM):
    """Return True if |rpm| is below the safety limit."""
    return abs(rpm) <= limit

# ── MOTOR CONTROLLER INITIALIZATION ─────────────────────────────────────────────
try:
    pythoncom.CoInitialize()
    mnt_ctrl = win32com.client.Dispatch("MintControls_5864.MintController.1")
    mnt_ctrl.SetUSBControllerLink(MOTOR_NODE_ID)
    motor_error_msg = None
except Exception as e:
    mnt_ctrl = None
    motor_error_msg = f"Motor init error: {e}"

if mnt_ctrl and mnt_ctrl.ErrorPresent:
    print("Clearing axis error...")
    mnt_ctrl.DoErrorClear(0, 0)
    time.sleep(0.25)  # Give a moment for it to clear

# ── FORCE AI0 INTO DIFFERENTIAL MODE ────────────────────────────────────────────
# If AI0 truly supports differential, uncomment the next two lines. Otherwise,
# move your sensor to a channel pair that does support differential.
# ul.a_input_mode(BOARD_NUM, AnalogInputMode.DIFFERENTIAL)
# time.sleep(0.05)

# ── THREAD CONTROL FLAG ─────────────────────────────────────────────────────────
reading = False  # When True, the DAQ-reading thread keeps running
daq_thread = None  # Reference to the background reading thread

# ── READ MODE CONTROL ───────────────────────────────────────────────────────────
# This StringVar determines which unit to display.
read_mode_var = None

# ── DAQ FUNCTIONS ───────────────────────────────────────────────────────────────
def read_daq_voltage():
    """
    Perform a 32-bit A/D read on CHANNEL and convert to volts.
    """
    raw32 = ul.a_in_32(BOARD_NUM, CHANNEL, VOLTAGE_RANGE)
    volts = ul.to_eng_units_32(BOARD_NUM, VOLTAGE_RANGE, raw32)
    return volts

def update_value():
    """
    Background thread: continuously read DAQ, maintain a ramping buffer of up
    to ROLLING_PERIOD points, compute the rolling average, and update label_var.
    Sleeps SAMPLE_INTERVAL between reads to avoid busy-wait.
    """
    pythoncom.CoInitialize()
    voltage_buffer = []  # store up to ROLLING_PERIOD readings
    motor_ctrl = None
    if mnt_ctrl is not None:
        try:
            motor_ctrl = win32com.client.Dispatch("MintControls_5864.MintController.1")
            motor_ctrl.SetUSBControllerLink(MOTOR_NODE_ID)
        except Exception:
            motor_ctrl = None

    while reading:
        try:
            # Small offset ensures zero-load voltage reads as 0.0
            newest = read_daq_voltage() - 0.011507
        except Exception as e:
            label_var.set(f"Error: {e}")
            break

        # Record raw data with timestamp
        if log_start_time is not None:
            elapsed = time.time() - log_start_time
            log_time.append(elapsed)
            log_voltage.append(newest)
            mass_kg_sample = 64.0690359 * math.sqrt(newest ** 2) - 0.0152348083
            log_mass_kg.append(mass_kg_sample)
            log_force_lbf.append(volts_to_force_lbf(newest))

        # Append new reading and clamp to ROLLING_PERIOD
        voltage_buffer.append(newest)
        if len(voltage_buffer) > ROLLING_PERIOD:
            voltage_buffer.pop(0)

        # Compute average over however many points we have (1 to ROLLING_PERIOD)
        avg_volts = sum(voltage_buffer) / len(voltage_buffer)

        # Display according to selected mode
        mode = read_mode_var.get()
        if mode == "Volts":
            label_var.set(f"Voltage: {avg_volts:.3f} V")
        else:
            # Convert averaged voltage into mass in kilograms and clamp at 0
            mass_kg = 64.0690359 * abs(avg_volts) - 0.0152348083
            if mass_kg < 0:
                mass_kg = 0.0
            force_n = mass_kg * 9.81
            pounds_force = force_n / 4.448

            # Apply torque feedback control using pounds-force if enabled
            global current_rpm
            if motor_ctrl is not None and torque_control_var.get():
                current_rpm = pid_torque_control(
                    motor_ctrl,
                    torque_setpoint_var.get(),
                    pounds_force,
                    current_rpm,
                )

                if abs(current_rpm) >= SAFETY_MAX_RPM:
                    motor_status_var.set("Safety stop: speed limit exceeded")
                    try:
                        motor_ctrl.DoStop(AXIS_ID)
                    except Exception:
                        pass
                    current_rpm = 0
                    torque_control_var.set(False)

            if mode == "Kilograms":
                label_var.set(f"Mass: {mass_kg:.2f} kg")
            elif mode == "Newtons":
                label_var.set(f"Force: {force_n:.2f} N")
            else:  # Pounds-Force
                label_var.set(f"Force: {pounds_force:.2f} Lbf")

        time.sleep(SAMPLE_INTERVAL)

    if motor_ctrl is not None:
        try:
            motor_ctrl.SetNullLink()
        except Exception:
            pass
    pythoncom.CoUninitialize()

def start_reading():
    """Start the DAQ-reading thread and reset the data logger."""
    global reading, daq_thread
    global log_start_time, log_time, log_voltage, log_mass_kg, log_force_lbf
    if not reading:
        # Reset logging arrays each time DAQ starts
        log_start_time = time.time()
        log_time = []
        log_voltage = []
        log_mass_kg = []
        log_force_lbf = []

        reading = True
        daq_thread = threading.Thread(target=update_value, daemon=True)
        daq_thread.start()

def stop_reading():
    """Signal the DAQ-reading thread to stop and prompt to save the log."""
    global reading, daq_thread
    reading = False
    if daq_thread is not None:
        daq_thread.join(timeout=1.0)
        daq_thread = None
    prompt_save_data()

def prompt_save_data():
    """Ask the user for a filename and save logged data to Excel."""
    if not log_time:
        return  # nothing recorded

    try:
        import pandas as pd
        import tkinter.filedialog as fd
    except Exception:
        return

    fname = fd.asksaveasfilename(
        defaultextension=".xlsx",
        filetypes=[("Excel files", "*.xlsx"), ("All files", "*.*")],
    )
    if not fname:
        return

    df = pd.DataFrame(
        {
            "Time (s)": log_time,
            "Voltage (V)": log_voltage,
            "Torque Conversion": log_mass_kg,
            "Force (lbf)": log_force_lbf,
        }
    )
    try:
        df.to_excel(fname, index=False)
    except Exception:
        pass

# ── MOTOR UTILITY ───────────────────────────────────────────────────────────────
def _ensure_motor_and_handle_error():
    """
    If motor controller failed to initialize, return False and set status.
    Otherwise, return True.
    """
    if mnt_ctrl is None:
        motor_status_var.set(motor_error_msg)
        return False
    return True

# ── MOTOR CONTROL FUNCTIONS ─────────────────────────────────────────────────────
def enable_motor():
    if not _ensure_motor_and_handle_error():
        return
    try:
        if not mnt_ctrl.DriveEnable(AXIS_ID):
            mnt_ctrl.SetDriveEnable(AXIS_ID, True)
        motor_status_var.set("Motor: Enabled")
    except Exception as e:
        motor_status_var.set(f"Error: {e}")

def disable_motor():
    if not _ensure_motor_and_handle_error():
        return
    try:
        mnt_ctrl.SetDriveEnable(AXIS_ID, False)
        motor_status_var.set("Motor: Disabled")
    except Exception as e:
        motor_status_var.set(f"Error: {e}")

def get_jog_steps_per_sec():
    """Convert the UI speed/units into steps per second."""
    val = speed_var.get()
    if unit_var.get() == "RPM":
        return val / 6.0
    return val / DEGREES_PER_STEP


def jog_forward():
    if not _ensure_motor_and_handle_error():
        return
    try:
        global current_rpm
        current_rpm = speed_var.get() if unit_var.get() == "RPM" else speed_var.get() * 6.0
        mnt_ctrl.SetJog(AXIS_ID, get_jog_steps_per_sec())
        motor_status_var.set("Motor: Jogging Forward")
    except Exception as e:
        motor_status_var.set(f"Error: {e}")

def jog_backward():
    if not _ensure_motor_and_handle_error():
        return
    try:
        global current_rpm
        current_rpm = speed_var.get() if unit_var.get() == "RPM" else speed_var.get() * 6.0
        mnt_ctrl.SetJog(AXIS_ID, -get_jog_steps_per_sec())
        motor_status_var.set("Motor: Jogging Backward")
    except Exception as e:
        motor_status_var.set(f"Error: {e}")

def stop_motor():
    if not _ensure_motor_and_handle_error():
        return
    try:
        mnt_ctrl.DoStop(AXIS_ID)
        global current_rpm
        current_rpm = 0
        motor_status_var.set("Motor: Stopped")
    except Exception as e:
        motor_status_var.set(f"Error: {e}")

# ── CLEANUP FUNCTION ────────────────────────────────────────────────────────────
def on_closing():
    """
    Called when the window is closed. Stops the DAQ thread,
    disables motor, unlinks the controller, and exits.
    """
    global reading
    reading = False

    # Disable motor and remove USB link
    if mnt_ctrl:
        try:
            mnt_ctrl.SetDriveEnable(AXIS_ID, False)
            mnt_ctrl.SetNullLink()
        except Exception:
            pass

    root.destroy()
    pythoncom.CoUninitialize()

# ── TKINTER GUI SETUP ──────────────────────────────────────────────────────────
root = tk.Tk()
root.title("DAQ + Motor Control")
root.geometry("400x400")  # expanded to fit speed controls

# 1) Read-mode selection (choose display unit)
read_mode_var = tk.StringVar(value="Volts")
mode_frame = ttk.LabelFrame(root, text="Display Mode")
mode_frame.pack(pady=10, fill=tk.X, padx=20)

voltage_radio = ttk.Radiobutton(
    mode_frame, text="Volts", value="Volts", variable=read_mode_var
)
voltage_radio.pack(side=tk.LEFT, padx=5, pady=5)

lbf_radio = ttk.Radiobutton(
    mode_frame, text="Lbf", value="Pounds-Force", variable=read_mode_var
)
lbf_radio.pack(side=tk.LEFT, padx=5, pady=5)

kg_radio = ttk.Radiobutton(
    mode_frame, text="Kilograms", value="Kilograms", variable=read_mode_var
)
kg_radio.pack(side=tk.LEFT, padx=5, pady=5)

n_radio = ttk.Radiobutton(
    mode_frame, text="Newtons", value="Newtons", variable=read_mode_var
)
n_radio.pack(side=tk.LEFT, padx=5, pady=5)

# 2) Display label for sensor value
label_var = tk.StringVar(value="Reading: ---")
voltage_label = ttk.Label(root, textvariable=label_var, font=("Arial", 16))
voltage_label.pack(pady=10)

# 3) Frame for Start/Stop DAQ buttons
daq_frame = ttk.Frame(root)
daq_frame.pack(pady=5, fill=tk.X, padx=20)

start_daq_btn = ttk.Button(daq_frame, text="Start DAQ", command=start_reading)
start_daq_btn.pack(side=tk.LEFT, expand=True, padx=5)

stop_daq_btn = ttk.Button(daq_frame, text="Stop DAQ", command=stop_reading)
stop_daq_btn.pack(side=tk.RIGHT, expand=True, padx=5)

# 4) Horizontal separator before motor controls
sep = ttk.Separator(root, orient="horizontal")
sep.pack(fill="x", pady=10)

# Jog speed controls -------------------------------------------------------
speed_frame = ttk.Frame(root)
speed_frame.pack(pady=5, fill=tk.X, padx=20)

speed_var = tk.DoubleVar(value=DEFAULT_JOG_RPM)
unit_var = tk.StringVar(value="RPM")
speed_display_var = tk.StringVar()

def update_speed_display(*args):
    speed_display_var.set(
        f"Jog Speed: {speed_var.get():.1f} {unit_var.get()}"
    )

speed_var.trace_add("write", update_speed_display)
unit_var.trace_add("write", update_speed_display)
update_speed_display()

speed_spin = ttk.Spinbox(speed_frame, from_=0, to=10000, textvariable=speed_var, width=8)
speed_spin.grid(row=0, column=0, padx=5)

unit_menu = ttk.OptionMenu(speed_frame, unit_var, unit_var.get(), "RPM", "Degrees/sec")
unit_menu.grid(row=0, column=1, padx=5)

speed_label = ttk.Label(speed_frame, textvariable=speed_display_var)
speed_label.grid(row=0, column=2, padx=5)

# Torque feedback controls -----------------------------------------------
torque_control_var = tk.BooleanVar(value=False)
torque_setpoint_var = tk.DoubleVar(value=TORQUE_SETPOINT_LBF)

torque_frame = ttk.LabelFrame(root, text="Torque Feedback")
torque_frame.pack(pady=5, fill=tk.X, padx=20)

torque_check = ttk.Checkbutton(
    torque_frame, text="Enable", variable=torque_control_var
)
torque_check.grid(row=0, column=0, padx=5, pady=2, sticky="w")

ttk.Label(torque_frame, text="Setpoint Lbf:").grid(row=0, column=1, padx=5, pady=2)
torque_entry = ttk.Entry(torque_frame, textvariable=torque_setpoint_var, width=6)
torque_entry.grid(row=0, column=2, padx=5, pady=2)

# 5) Motor status label and variable
motor_status_var = tk.StringVar(
    value="Motor: Unknown" if mnt_ctrl else motor_error_msg
)
motor_status_label = ttk.Label(root, textvariable=motor_status_var, font=("Arial", 12))
motor_status_label.pack(pady=5)

# 6) Frame for motor control buttons
motor_frame = ttk.Frame(root)
motor_frame.pack(pady=5, fill=tk.X, padx=20)

# Enable / Disable buttons
enable_btn = ttk.Button(motor_frame, text="Enable Motor", command=enable_motor)
enable_btn.grid(row=0, column=0, padx=5, pady=2)

disable_btn = ttk.Button(motor_frame, text="Disable Motor", command=disable_motor)
disable_btn.grid(row=0, column=1, padx=5, pady=2)

# Jog forward / backward buttons
jog_fwd_btn = ttk.Button(motor_frame, text="Jog Forward", command=jog_forward)
jog_fwd_btn.grid(row=1, column=0, padx=5, pady=2)

jog_rev_btn = ttk.Button(motor_frame, text="Jog Backward", command=jog_backward)
jog_rev_btn.grid(row=1, column=1, padx=5, pady=2)

# 7) Stop motor button (spans full width)
stop_motor_btn = ttk.Button(root, text="Stop Motor", command=stop_motor)
stop_motor_btn.pack(pady=10)

# 8) Bind the window close event to cleanup
root.protocol("WM_DELETE_WINDOW", on_closing)

# 9) Start the Tkinter event loop
root.mainloop()
