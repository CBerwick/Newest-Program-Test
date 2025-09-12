# -*- coding: utf-8 -*-
"""
Shear Test App (MPC, ARX model + optional online adaptation)

ADDED v1.3.3:
  • "Mode" switch (Simplified vs Developer)
      - Simplified mode (default): shows ONLY
          * Start DAQ
          * Speed input
          * Enable Motor
          * Jog Forward / Jog Backward
          * Stop Motor
          * Clear Errors
          * Status + live readout label
      - Developer mode: restores ALL original widgets (Display unit selector,
        Start/Stop DAQ, MPC settings, Model panel, Live graph, etc.)
      - Switch via the "Mode" radio buttons at the top (Simplified / Developer),
        or press F12.

  • NEW "Cyclic (A ↔ B)" lbf option (Developer mode, under MPC Settings):
      - Enter A (lbf) and B (lbf) and a tolerance (±lbf).
      - Check "Cyclic (A ↔ B)". With "Enable MPC" ON, the controller will:
          1) drive to A; when |force - A| ≤ tol, switch target to B
          2) drive to B; when |force - B| ≤ tol, switch target to A
         …and repeat until you uncheck "Cyclic" or press "Stop Motor".
      - Pressing "Stop Motor" or "Disable Motor" automatically turns Cyclic OFF.

NOTE: All original control logic is intact; the new features only add UI/logic
      around the existing MPC setpoint.
"""

import tkinter as tk
from tkinter import ttk, filedialog
import threading, time, math, json, random, os, sqlite3, queue
from collections import deque
import numpy as np

# ------------- Optional QP solver (recommended). Fallback is analytic LS. ------------- 
try:
    import osqp  # pip install osqp
    _HAS_OSQP = True
except Exception:
    _HAS_OSQP = False

# ------------- Optional for quick TF printing (not required) ------------- 
try:
    import control as ct
except Exception:
    ct = None

# ------------- Hardware / DAQ ------------- 
from mcculw import ul
from mcculw.enums import ULRange
import win32com.client
import pythoncom
from daq_stream import DaqStream, DaqConfig

# ------------- Plotting ------------- 
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# ── CONFIG ─────────────────────────────────────────────────────────────────────
MOTOR_NODE_ID = 2
AXIS_ID = 0
DEGREES_PER_STEP = 36.0
DEFAULT_JOG_RPM = 5

# ------------- ARX bootstrap (your 30 rpm result) ------------- 
DEFAULT_DT = 0.05
DEFAULT_NK = 0
DEFAULT_A = [1.0, -0.9977585018924905]
DEFAULT_B = [0.0027434762818458382, -0.0020121025585734986]

# ------------- Safety & timing -------------
SAFETY_MAX_RPM = 30.0
SLEW_RATE_RPM_S = 120.0
COMMAND_DEADBAND_RPM = 0.2
SAMPLE_INTERVAL = 0.1
WATCHDOG_DT_MAX = SAMPLE_INTERVAL * 10.0

# ------------- Setpoint Initial Values -------------
# Base SETPOINT when MPC is enabled
SETPOINT_INITIAL = 50.0  # lbf

# ------------- Cyclic mode (A↔B) defaults ------------- 
CYCLIC_A_INITIAL = 45.0  # lbf
CYCLIC_B_INITIAL = 155.0  # lbf
CYCLIC_TOLERANCE = 5.0   # lbf

# ------------- Ramp mode defaults ------------- 
RAMP_TARGET_INITIAL = 800.0  # lbf
RAMP_TIME_INITIAL = 2.5      # minutes


# ------------- DAQ ------------- 
BOARD_NUM = 0
CHANNEL = 0
VOLTAGE_RANGE = ULRange.BIP10VOLTS

# ------------- Display smoothing (UI only; MPC uses raw) ------------- 
ROLLING_PERIOD = int(0.5 / SAMPLE_INTERVAL)

# ------------- Calibration (signed) ------------- 
def volts_to_force_lbf(v):
    # Adjust the offset if needed; this is your prior slope/offset without abs()
    return 261.19447705 * v + 0.00785081


# ── MPC (ARX) IMPLEMENTATION ───────────────────────────────────────────────────
class MPC_ARX:
    """
    SISO MPC on an ARX model:
      A(q^-1) y_k = B(q^-1) u_{k-nk}  (noise ignored in predictions)
    We optimize ΔU over horizon Nu to track r over Np with cost:
        J = ||Y_base + G ΔU - r_vec||_Q^2 + ||ΔU||_R^2
    Apply only Δu[0], then repeat (receding horizon).

    If OSQP present, enforce box constraints on Δu and u; else solve LS and
    rely on the outer safety filter to respect plant limits.
    """
    def __init__(self, dt, A, B, nk, Np=200, Nu=2, qy=0.5, rdu=12,
                 du_limit=None, u_min=None, u_max=None, use_osqp=False):
        self.set_model(dt, A, B, nk)
        self.set_weights(Np, Nu, qy, rdu)
        self.set_constraints(du_limit, u_min, u_max)
        self.use_osqp = use_osqp and _HAS_OSQP

        # history buffers (latest first)
        self.y_hist = [0.0] * (self.na)
        self.u_hist = [0.0] * (self.nk + self.nb)

        # OSQP workspace
        self._osqp = None
        self._last_shapes = None  # to rebuild if dims change

    # --- public API ---
    def set_model(self, dt, A, B, nk):
        self.dt = float(dt)
        self.A = np.array(A, dtype=float).flatten()
        self.B = np.array(B, dtype=float).flatten()
        assert abs(self.A[0] - 1.0) < 1e-9, "A must start with 1.0"
        self.na = len(self.A) - 1
        self.nb = len(self.B)
        self.nk = int(nk)

    def set_weights(self, Np, Nu, qy, rdu):
        self.Np = int(Np)
        self.Nu = int(Nu)
        self.qy = float(qy)
        self.rdu = float(rdu)

    def set_constraints(self, du_limit, u_min, u_max):
        # per-step move-rate limit (|Δu| ≤ du_limit), and absolute u bounds
        self.du_limit = float(du_limit) if du_limit is not None else None
        self.u_min = float(u_min) if u_min is not None else None
        self.u_max = float(u_max) if u_max is not None else None

    def reset(self):
        self.y_hist = [0.0] * (self.na)
        self.u_hist = [0.0] * (self.nk + self.nb)

    def step(self, y_meas, r, u_prev):
        """Compute next control (RPM) from measurement y and target r."""
        # roll in newest data
        self.y_hist = [float(y_meas)] + self.y_hist[:self.na-1]
        self.u_hist = [float(u_prev)] + self.u_hist[:(self.nk + self.nb - 1)]

        # Build base prediction with future Δu = 0 (hold u_prev)
        y_base = self._predict_base(u_hold=u_prev)

        # Build dynamic matrix G (sensitivity wrt future Δu moves)
        G = self._build_G(u_prev)

        # Target vector
        r_vec = np.full((self.Np, 1), float(r))

        # Solve for ΔU
        dU = self._solve_qp(G, y_base, r_vec, u_prev)

        # Apply only the first move
        du0 = float(dU[0]) if dU.size else 0.0
        # If we don't use internal QP constraints, at least respect move-rate here
        if not self.use_osqp and self.du_limit is not None:
            du0 = max(min(du0, self.du_limit), -self.du_limit)
        u_cmd = u_prev + du0
        return u_cmd

    # --- internals ---
    def _arx_step(self, y_state, u_state, u_now):
        """
        One-step ARX prediction given current y_state (size na), u_state (size nk+nb),
        and a candidate input u_now (scalar). Returns y_next and updated states.
        States are stored newest-first.
        """
        # y_next = -sum_{i=1..na} a_i*y[k-i] + sum_{j=1..nb} b_j*u[k-nk-j+1]
        acc = 0.0
        # output part
        for i in range(1, self.na + 1):
            acc += -self.A[i] * y_state[i - 1]
        # input part (nk delay)
        # shift u_state: it stores [u[k-0], u[k-1], ...] of length nk+nb
        u_state = [u_now] + u_state[:-1]
        for j in range(1, self.nb + 1):
            idx = self.nk + j - 1
            acc += self.B[j - 1] * u_state[idx]
        y_next = acc
        # update y_state
        y_state = [y_next] + y_state[:-1]
        return y_next, y_state, u_state

    def _predict_base(self, u_hold):
        """Free response with future Δu=0 (hold u_hold)."""
        y_state = self.y_hist.copy()
        u_state = self.u_hist.copy()
        y_out = np.zeros((self.Np, 1))
        u_future = float(u_hold)
        for k in range(self.Np):
            yk1, y_state, u_state = self._arx_step(y_state, u_state, u_future)
            y_out[k, 0] = yk1
        return y_out

    def _build_G(self, u_prev):
        """
        Numerical sensitivity: columns are the output change from a unit Δu at
        future steps j=0..Nu-1, with hold afterwards.
        """
        G = np.zeros((self.Np, self.Nu))
        for j in range(self.Nu):
            y_state = self.y_hist.copy()
            u_state = self.u_hist.copy()
            u_future = float(u_prev)
            incr = 0.0
            for k in range(self.Np):
                if k >= j:
                    incr = 1.0  # cumulative (Δu is integrated into u)
                yk1, y_state, u_state = self._arx_step(y_state, u_state, u_future + incr)
                G[k, j] = yk1
        base = self._predict_base(u_hold=u_prev).flatten()
        G = G - base.reshape(-1, 1)
        return G

    def _solve_qp(self, G, y_base, r_vec, u_prev):
        """
        Solve: min 0.5 x^T P x + q^T x, x = ΔU, with (optional) box constraints.
        P = 2(G'QG + R), q = -2 G'Q (r - y_base)
        """
        Q = self.qy * np.eye(self.Np)
        R = self.rdu * np.eye(self.Nu)
        P = G.T @ Q @ G + R
        q = -G.T @ Q @ (r_vec - y_base)

        # Constraints (if OSQP): |Δu| ≤ du_limit; u_min ≤ u_prev + cumsum(ΔU) ≤ u_max
        # inside MPC_ARX._solve_qp (after P, q are built)
        if self.use_osqp and (self.du_limit is not None or self.u_min is not None or self.u_max is not None):
            from scipy import sparse
            S = np.tril(np.ones((self.Nu, self.Nu)))
            A_parts, l_parts, u_parts = [], [], []
        
            # Δu box: [ I ; -I ] ΔU  ∈ [ -lim ; -lim ] .. [ +lim ; +lim ]
            if self.du_limit is not None:
                I = np.eye(self.Nu)
                lim = float(self.du_limit) * np.ones((self.Nu, 1))
                A_parts.append(np.vstack((I, -I)))
                l_parts.append(np.vstack((-lim, -lim)))
                u_parts.append(np.vstack((+lim, +lim)))
        
            # Absolute u box at first few steps (keep it light so solve stays fast)
            rows = min(2, self.Nu)
            Srows = S[:rows, :]
            if self.u_max is not None:
                A_parts.append(Srows)
                l_parts.append(-np.inf * np.ones((rows, 1)))
                u_parts.append((self.u_max - u_prev) * np.ones((rows, 1)))
            if self.u_min is not None:
                A_parts.append(-Srows)
                l_parts.append(-np.inf * np.ones((rows, 1)))
                u_parts.append((u_prev - self.u_min) * np.ones((rows, 1)))
        
            # Stack & solve (with a quick, safe fallback)
            A = np.vstack(A_parts)
            l = np.vstack(l_parts).ravel()
            u = np.vstack(u_parts).ravel()
        
            Psp = sparse.csc_matrix(0.5*(P + P.T))
            Asp = sparse.csc_matrix(A)
        
            m = osqp.OSQP()
            try:
                m.setup(P=Psp, q=q.ravel(), A=Asp, l=l, u=u, verbose=False, max_iter=200)
            except TypeError:
                m.setup(P=Psp, q=q.ravel(), A=Asp, l=l, u=u, verbose=False)
        
            sol = m.solve()
            if (sol.x is None) or (getattr(sol, "info", None) and getattr(sol.info, "status_val", 0) not in (1, 2)):
                # Fall back to unconstrained for this tick so the loop never dies
                dU = np.linalg.solve(P + 1e-8*np.eye(self.Nu), -q)
            else:
                dU = sol.x.reshape(-1, 1)
        else:
            dU = np.linalg.solve(P + 1e-8*np.eye(self.Nu), -q)
        
        return dU.flatten()



# ── Online ARX adaptation (RLS) ────────────────────────────────────────────────
class RLS_ARX:
    """Simple RLS for SISO ARX with forgetting factor."""
    def __init__(self, na, nb, nk, lam=0.99, P0=1e4):
        self.na, self.nb, self.nk = na, nb, nk
        self.lam = lam
        self.theta = np.zeros((na+nb, 1))  # [a1..ana, b1..bnb]^T
        self.P = P0 * np.eye(na+nb)

        # histories (newest first)
        self.y_hist = [0.0]*na
        self.u_hist = [0.0]*(nk+nb)

    def update(self, y, u):
        """
        One-step RLS update for ARX:
          y[k] ≈ [-y[k-1] ... -y[k-na]  u[k-1-nk] ... u[k-nk-nb]] · theta
        IMPORTANT: build phi from PAST samples (what's already in the histories),
                   THEN push the current (y,u) into the histories.
        """
        # 1) Build regressor from histories that hold PAST samples
        phi = []
        for i in range(1, self.na + 1):
            phi.append(-self.y_hist[i - 1])
        for j in range(1, self.nb + 1):
            idx = self.nk + j - 1
            phi.append(self.u_hist[idx] if idx < len(self.u_hist) else 0.0)
        phi = np.array(phi, dtype=float).reshape(-1, 1)

        # 2) RLS math
        Pphi = self.P @ phi
        alpha = float(self.lam + (phi.T @ Pphi).item())
        K = Pphi / alpha
        y_hat = float((phi.T @ self.theta).item())
        eps = float(y - y_hat)
        self.theta = self.theta + K * eps
        self.P = (self.P - K @ phi.T @ self.P) / self.lam

        # 3) Push CURRENT samples for next call
        self.y_hist = [float(y)] + self.y_hist[:-1]
        self.u_hist = [float(u)] + self.u_hist[:-1]

        # 4) Extract model; keep A[0]=1 and clamp to near-stable
        a = self.theta[:self.na, 0].copy()
        b = self.theta[self.na:, 0].copy()
        a = np.clip(a, -0.9999, 0.9999)
        return [1.0, *a.tolist()], b.tolist()


# ── App ────────────────────────────────────────────────────────────────────────
class DAQMotorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Shear Test (MPC + ARX)")

        # State
        self.reading = False
        self.read_thread = None
        self.current_rpm = 0.0
        self._last_cmd_rpm = 0.0
        self._last_cmd_time = None

        # Histories (for display). Use bounded deques to avoid unbounded memory growth.
        # Full persistent logging is written to SQLite to survive crashes and long runs.
        MAX_IN_MEMORY = 10000
        self._hist_lock = threading.Lock()
        self.lbf_history = deque(maxlen=MAX_IN_MEMORY)
        self.voltage_history = deque(maxlen=MAX_IN_MEMORY)
        self.time_history = deque(maxlen=MAX_IN_MEMORY)
        self.graph_start_time = time.time()
        self.update_graph_interval = 200  # ms

        self.log_start_time = None
        # In-memory recent run buffers (bounded). Persistent storage below.
        self.log_time = deque(maxlen=MAX_IN_MEMORY)
        self.log_voltage = deque(maxlen=MAX_IN_MEMORY)
        self.log_mass_kg = deque(maxlen=MAX_IN_MEMORY)
        self.log_force_lbf = deque(maxlen=MAX_IN_MEMORY)

        # Persistent logging (SQLite writer)
        self._db_conn = None
        self._db_queue = queue.Queue()
        self._db_writer_thread = None
        self._db_stop_event = threading.Event()
        self._db_path = None
        # DB rotation config (size in bytes). 200 MB default.
        self._db_max_bytes = 200 * 1024 * 1024
        self._db_rotate_count = 0

        # Identification capture
        self._id_running = False
        self._id_u, self._id_y, self._id_t = [], [], []
        self._id_dt = SAMPLE_INTERVAL

        # MPC objects
        self.mpc = MPC_ARX(
            dt=DEFAULT_DT, A=DEFAULT_A, B=DEFAULT_B, nk=DEFAULT_NK,
            Np=200, Nu=2, qy=0.5, rdu=12,
            du_limit=SLEW_RATE_RPM_S * SAMPLE_INTERVAL,
            u_min=-SAFETY_MAX_RPM, u_max=SAFETY_MAX_RPM,
            use_osqp=True  # try OSQP if installed
        )
        self.rls = RLS_ARX(na=len(DEFAULT_A)-1, nb=len(DEFAULT_B), nk=DEFAULT_NK, lam=0.995)
        # Try to load default model settings from tests/smooth_ramp.json on first run
        try:
            self._load_default_model()
        except Exception:
            pass

        # COM motor
        pythoncom.CoInitialize()
        try:
            self.mnt_ctrl = win32com.client.Dispatch("MintControls_5864.MintController.1")
            self.mnt_ctrl.SetUSBControllerLink(MOTOR_NODE_ID)
            if self.mnt_ctrl.ErrorPresent:
                self.mnt_ctrl.DoErrorClear(0, 0)
                time.sleep(0.25)
            self.motor_error_msg = None
        except Exception as e:
            self.mnt_ctrl = None
            self.motor_error_msg = f"Motor init error: {e}"
            pythoncom.CoUninitialize()
            self.destroy()
            return

        # UI Mode state (NEW)
        self.mode_var = tk.StringVar(value="Simplified")  # default per request

        # Cyclic state (NEW)
        self._cyclic_target = None   # active numeric target when cyclic is on
        self._cyclic_side = None     # 'A' or 'B'
        # Cyclic guard (internal): require short dwell and low slope at target
        self._cyclic_last_switch_t = None
        self._cyclic_slope_ema = None
        self._cyclic_dwell_s = 0.7           # seconds within which we avoid re-switching
        self._cyclic_slope_limit = 8.0       # lbf/s max slope allowed to switch

        # Ramp-to-target state (NEW)
        self._ramp_start_time = None
        self._ramp_start_load = None
        self._ramp_target = None
        self._ramp_rate = None

        self._build_gui()
        self._apply_mode()  # start in Simplified mode
        # start periodic DB UI update
        self.after(1000, self._update_db_ui)
        self.bind("<F12>", lambda e: self._toggle_mode())
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

    # --- GUI ---
    def _build_gui(self):
        # ── Top toolbar: Mode switch (NEW) ─────────────────────────────────────
        self.toolbar = ttk.Frame(self); self.toolbar.pack(pady=6, fill=tk.X, padx=16)
        ttk.Label(self.toolbar, text="Mode:").pack(side=tk.LEFT, padx=(0,6))
        ttk.Radiobutton(self.toolbar, text="Simplified", value="Simplified",
                        variable=self.mode_var, command=self._apply_mode).pack(side=tk.LEFT, padx=2)
        ttk.Radiobutton(self.toolbar, text="Developer", value="Developer",
                        variable=self.mode_var, command=self._apply_mode).pack(side=tk.LEFT, padx=2)
        ttk.Label(self.toolbar, text="(Press F12 to toggle)").pack(side=tk.LEFT, padx=10)

        # Display unit selector (DEV)
        # Only pounds-force is supported; other units have been removed.
        self.read_mode_var = tk.StringVar(value="Pounds-Force")
        self.display_frame = ttk.LabelFrame(self, text="Display")
        self.display_frame.pack(pady=6, fill=tk.X, padx=16)
        ttk.Radiobutton(self.display_frame, text="Pounds-Force",
                        value="Pounds-Force",
                        variable=self.read_mode_var).pack(side=tk.LEFT, padx=5)
        # Live label (both modes)
        self.label_var = tk.StringVar(value="Reading: ---")
        self.live_label = ttk.Label(self, textvariable=self.label_var, font=("Arial", 16))
        self.live_label.pack(pady=6)

        # DAQ controls (Start always visible; Stop = DEV)
        self.daq_frame = ttk.Frame(self); self.daq_frame.pack(pady=4, fill=tk.X, padx=16)
        self.btn_start_daq = ttk.Button(self.daq_frame, text="Start DAQ", command=self.start_reading)
        self.btn_start_daq.pack(side=tk.LEFT, expand=True, padx=4)
        self.btn_stop_daq = ttk.Button(self.daq_frame, text="Stop DAQ", command=self.stop_reading)
        self.btn_stop_daq.pack(side=tk.RIGHT, expand=True, padx=4)

        # Separator (DEV)
        self.sep_main = ttk.Separator(self, orient="horizontal")
        self.sep_main.pack(fill="x", pady=6)

        # MPC settings (DEV)
        self.mpc_frame = ttk.LabelFrame(self, text="MPC Settings")
        self.mpc_frame.pack(pady=6, fill=tk.X, padx=16)
        self.mpc_enable = tk.BooleanVar(value=False)
        self.torque_setpoint_var = tk.DoubleVar(value=SETPOINT_INITIAL)
        # Keep thread-safe Python snapshots of Tk variables so background threads
        # don't call tk.Variable.get() (tk is not thread-safe). Update via trace.
        # torque setpoint
        self._last_torque_setpoint = float(self.torque_setpoint_var.get())
        def _trace_setpoint(*args):
            try:
                self._last_torque_setpoint = float(self.torque_setpoint_var.get())
            except Exception:
                pass
        try:
            self.torque_setpoint_var.trace_add('write', _trace_setpoint)
        except Exception:
            try:
                self.torque_setpoint_var.trace('w', _trace_setpoint)
            except Exception:
                pass

        # mpc_enable
        self._last_mpc_enable = bool(self.mpc_enable.get())
        def _trace_mpc_enable(*args):
            try:
                self._last_mpc_enable = bool(self.mpc_enable.get())
            except Exception:
                pass
        try:
            self.mpc_enable.trace_add('write', _trace_mpc_enable)
        except Exception:
            try:
                self.mpc_enable.trace('w', _trace_mpc_enable)
            except Exception:
                pass
        self.Np_var = tk.IntVar(value=self.mpc.Np)
        self.Nu_var = tk.IntVar(value=self.mpc.Nu)
        self.qy_var = tk.DoubleVar(value=self.mpc.qy)
        self.rdu_var = tk.DoubleVar(value=self.mpc.rdu)
        # Start RLS automatic adjustment UNCHECKED by default on first run
        self.adapt_var = tk.BooleanVar(value=False)
        # Thread-safe snapshots for MPC params used by background thread
        self._last_Np = int(self.Np_var.get())
        self._last_Nu = int(self.Nu_var.get())
        self._last_qy = float(self.qy_var.get())
        self._last_rdu = float(self.rdu_var.get())
        self._last_adapt = bool(self.adapt_var.get())
        def _trace_Np(*_):
            try:
                self._last_Np = int(self.Np_var.get())
            except Exception:
                pass
        def _trace_Nu(*_):
            try:
                self._last_Nu = int(self.Nu_var.get())
            except Exception:
                pass
        def _trace_qy(*_):
            try:
                self._last_qy = float(self.qy_var.get())
            except Exception:
                pass
        def _trace_rdu(*_):
            try:
                self._last_rdu = float(self.rdu_var.get())
            except Exception:
                pass
        def _trace_adapt(*_):
            try:
                self._last_adapt = bool(self.adapt_var.get())
            except Exception:
                pass
        try:
            self.Np_var.trace_add('write', _trace_Np)
            self.Nu_var.trace_add('write', _trace_Nu)
            self.qy_var.trace_add('write', _trace_qy)
            self.rdu_var.trace_add('write', _trace_rdu)
            self.adapt_var.trace_add('write', _trace_adapt)
        except Exception:
            try:
                self.Np_var.trace('w', _trace_Np)
                self.Nu_var.trace('w', _trace_Nu)
                self.qy_var.trace('w', _trace_qy)
                self.rdu_var.trace('w', _trace_rdu)
                self.adapt_var.trace('w', _trace_adapt)
            except Exception:
                pass

        ttk.Checkbutton(self.mpc_frame, text="Enable MPC", variable=self.mpc_enable).grid(row=0, column=0, padx=4)
        ttk.Label(self.mpc_frame, text="Setpoint (lbf):").grid(row=0, column=1, padx=4)
        ttk.Entry(self.mpc_frame, width=7, textvariable=self.torque_setpoint_var).grid(row=0, column=2, padx=4)
        ttk.Label(self.mpc_frame, text="Np:").grid(row=0, column=3, padx=4)
        ttk.Entry(self.mpc_frame, width=5, textvariable=self.Np_var).grid(row=0, column=4, padx=2)
        ttk.Label(self.mpc_frame, text="Nu:").grid(row=0, column=5, padx=4)
        ttk.Entry(self.mpc_frame, width=5, textvariable=self.Nu_var).grid(row=0, column=6, padx=2)
        ttk.Label(self.mpc_frame, text="Qy:").grid(row=0, column=7, padx=4)
        ttk.Entry(self.mpc_frame, width=6, textvariable=self.qy_var).grid(row=0, column=8, padx=2)
        ttk.Label(self.mpc_frame, text="Rdu:").grid(row=0, column=9, padx=4)
        ttk.Entry(self.mpc_frame, width=6, textvariable=self.rdu_var).grid(row=0, column=10, padx=2)
        ttk.Checkbutton(self.mpc_frame, text="Adapt model (RLS)", variable=self.adapt_var).grid(row=0, column=11, padx=6)

        # --- NEW: Cyclic A↔B controls ---
        self.cyclic_var = tk.BooleanVar(value=False)
        self.cyclic_a_var = tk.DoubleVar(value=CYCLIC_A_INITIAL)
        self.cyclic_b_var = tk.DoubleVar(value=CYCLIC_B_INITIAL)
        self.cyclic_tol_var = tk.DoubleVar(value=CYCLIC_TOLERANCE)

        # cyclic snapshots
        self._last_cyclic = bool(self.cyclic_var.get())
        self._last_cyclic_a = float(self.cyclic_a_var.get())
        self._last_cyclic_b = float(self.cyclic_b_var.get())
        self._last_cyclic_tol = float(self.cyclic_tol_var.get())
        def _trace_cyclic(*args):
            try:
                self._last_cyclic = bool(self.cyclic_var.get())
            except Exception:
                pass
        def _trace_cyclic_a(*args):
            try:
                self._last_cyclic_a = float(self.cyclic_a_var.get())
            except Exception:
                pass
        def _trace_cyclic_b(*args):
            try:
                self._last_cyclic_b = float(self.cyclic_b_var.get())
            except Exception:
                pass
        def _trace_cyclic_tol(*args):
            try:
                self._last_cyclic_tol = float(self.cyclic_tol_var.get())
            except Exception:
                pass
        try:
            self.cyclic_var.trace_add('write', _trace_cyclic)
            self.cyclic_a_var.trace_add('write', _trace_cyclic_a)
            self.cyclic_b_var.trace_add('write', _trace_cyclic_b)
            self.cyclic_tol_var.trace_add('write', _trace_cyclic_tol)
        except Exception:
            try:
                self.cyclic_var.trace('w', _trace_cyclic)
                self.cyclic_a_var.trace('w', _trace_cyclic_a)
                self.cyclic_b_var.trace('w', _trace_cyclic_b)
                self.cyclic_tol_var.trace('w', _trace_cyclic_tol)
            except Exception:
                pass

        ttk.Checkbutton(self.mpc_frame, text="Cyclic (A ↔ B)", variable=self.cyclic_var,
                        command=self._on_cyclic_toggle).grid(row=1, column=0, padx=4, sticky="w")
        ttk.Label(self.mpc_frame, text="A (lbf):").grid(row=1, column=1, padx=4, sticky="e")
        ttk.Entry(self.mpc_frame, width=7, textvariable=self.cyclic_a_var).grid(row=1, column=2, padx=2)
        ttk.Label(self.mpc_frame, text="B (lbf):").grid(row=1, column=3, padx=4, sticky="e")
        ttk.Entry(self.mpc_frame, width=7, textvariable=self.cyclic_b_var).grid(row=1, column=4, padx=2)
        ttk.Label(self.mpc_frame, text="Tol (±lbf):").grid(row=1, column=5, padx=4, sticky="e")
        ttk.Entry(self.mpc_frame, width=6, textvariable=self.cyclic_tol_var).grid(row=1, column=6, padx=2)

        # --- NEW: Ramp to target controls ---
        self.ramp_var = tk.BooleanVar(value=False)
        self.ramp_target_var = tk.DoubleVar(value=RAMP_TARGET_INITIAL)
        self.ramp_time_var = tk.DoubleVar(value=RAMP_TIME_INITIAL)  # minutes
        # ramp snapshots (for simplified UI read by background thread if needed)
        self._last_ramp = bool(self.ramp_var.get())
        self._last_ramp_target = float(self.ramp_target_var.get())
        self._last_ramp_time = float(self.ramp_time_var.get())
        def _trace_ramp(*args):
            try:
                self._last_ramp = bool(self.ramp_var.get())
                self._last_ramp_target = float(self.ramp_target_var.get())
                self._last_ramp_time = float(self.ramp_time_var.get())
            except Exception:
                pass
        try:
            self.ramp_var.trace_add('write', _trace_ramp)
            self.ramp_target_var.trace_add('write', _trace_ramp)
            self.ramp_time_var.trace_add('write', _trace_ramp)
        except Exception:
            try:
                self.ramp_var.trace('w', _trace_ramp)
            except Exception:
                pass
        ttk.Checkbutton(self.mpc_frame, text="Ramp to Target", variable=self.ramp_var,
                        command=self._on_ramp_toggle).grid(row=2, column=0, padx=4, sticky="w")
        ttk.Label(self.mpc_frame, text="Target (lbf):").grid(row=2, column=1, padx=4, sticky="e")
        ttk.Entry(self.mpc_frame, width=7, textvariable=self.ramp_target_var).grid(row=2, column=2, padx=2)
        ttk.Label(self.mpc_frame, text="Time (min):").grid(row=2, column=3, padx=4, sticky="e")
        ttk.Entry(self.mpc_frame, width=6, textvariable=self.ramp_time_var).grid(row=2, column=4, padx=2)

        # Model panel (DEV)
        self.model_frame = ttk.LabelFrame(self, text="Model (ARX)")
        self.model_frame.pack(pady=6, fill=tk.X, padx=16)
        self.model_lbl = tk.StringVar(value=self._model_str())
        ttk.Label(self.model_frame, textvariable=self.model_lbl).grid(row=0, column=0, columnspan=6, sticky="w", padx=4)
        ttk.Button(self.model_frame, text="Load Model JSON", command=self._load_model_json).grid(row=1, column=0, padx=4, pady=4)
        ttk.Button(self.model_frame, text="Save Model JSON", command=self._save_model_json).grid(row=1, column=1, padx=4)
        ttk.Button(self.model_frame, text="Reset Model", command=self._reset_model).grid(row=1, column=2, padx=4)
        ttk.Button(self.model_frame, text="Identify (ARX)", command=self._start_arx_id).grid(row=1, column=3, padx=6)

        # Graph (DEV)
        self.graph_frame = ttk.Frame(self); self.graph_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
        fig = Figure(figsize=(5,3))
        self.ax = fig.subplots()
        self.ax.set_title('Live Force / Voltage')
        self.ax.set_xlabel('Time (s)')
        self.ax.set_ylabel('Value')
        self.canvas = FigureCanvasTkAgg(fig, master=self.graph_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        # Speed controls (both modes)
        self.spd_frame = ttk.Frame(self); self.spd_frame.pack(pady=4, fill=tk.X, padx=16)
        self.speed_var = tk.DoubleVar(value=DEFAULT_JOG_RPM)
        self.unit_var = tk.StringVar(value="RPM")
        self.speed_display_var = tk.StringVar()
        self.speed_var.trace_add("write", self._update_speed_display)
        self.unit_var.trace_add("write", self._update_speed_display)
        ttk.Spinbox(self.spd_frame, from_=0, to=10000, textvariable=self.speed_var, width=8).grid(row=0, column=0, padx=5)
        ttk.OptionMenu(self.spd_frame, self.unit_var, self.unit_var.get(), "RPM", "Degrees/sec").grid(row=0, column=1, padx=5)
        ttk.Label(self.spd_frame, textvariable=self.speed_display_var).grid(row=0, column=2, padx=5)
        self._update_speed_display()

        # Simplified MPC controls (minimal setpoint + cyclic) - shown in Simplified mode
        self.simple_mpc_frame = ttk.Frame(self)
        ttk.Checkbutton(self.simple_mpc_frame, text="Enable Setpoint Mode", variable=self.mpc_enable).grid(row=0, column=0, padx=4)        
        ttk.Label(self.simple_mpc_frame, text="Setpoint (lbf):").grid(row=0, column=1, padx=4, sticky="e")
        ttk.Entry(self.simple_mpc_frame, width=8, textvariable=self.torque_setpoint_var).grid(row=0, column=2, padx=2)
        

        # Cyclic minimal inputs
        ttk.Checkbutton(self.simple_mpc_frame, text="Cyclic (A↔B)", variable=self.cyclic_var,
                        command=self._on_cyclic_toggle).grid(row=1, column=0, padx=4, sticky="w")
        ttk.Label(self.simple_mpc_frame, text="A:").grid(row=1, column=1, padx=2, sticky="e")
        ttk.Entry(self.simple_mpc_frame, width=7, textvariable=self.cyclic_a_var).grid(row=1, column=2, padx=2)
        ttk.Label(self.simple_mpc_frame, text="B:").grid(row=1, column=3, padx=2, sticky="e")
        ttk.Entry(self.simple_mpc_frame, width=7, textvariable=self.cyclic_b_var).grid(row=1, column=4, padx=2)
        ttk.Label(self.simple_mpc_frame, text="Tol:").grid(row=1, column=5, padx=2, sticky="e")
        ttk.Entry(self.simple_mpc_frame, width=6, textvariable=self.cyclic_tol_var).grid(row=1, column=6, padx=2)
        # Reset Graph button (visual only)
        ttk.Button(self.simple_mpc_frame, text="Reset Graph", command=self.reset_graph).grid(row=0, column=7, padx=6)

        # Simplified Ramp-to-target controls (reuse existing vars/handlers)
        ttk.Checkbutton(self.simple_mpc_frame, text="Ramp to Target", variable=self.ramp_var,
                        command=self._on_ramp_toggle).grid(row=2, column=0, padx=4, sticky="w")
        ttk.Label(self.simple_mpc_frame, text="Target(lbf):").grid(row=2, column=1, padx=2, sticky="e")
        ttk.Entry(self.simple_mpc_frame, width=7, textvariable=self.ramp_target_var).grid(row=2, column=2, padx=2)
        ttk.Label(self.simple_mpc_frame, text="Time(min):").grid(row=2, column=3, padx=2, sticky="e")
        ttk.Entry(self.simple_mpc_frame, width=6, textvariable=self.ramp_time_var).grid(row=2, column=4, padx=2)

        # Status (both modes)
        status = "Motor: OK" if self.mnt_ctrl else self.motor_error_msg
        self.motor_status_var = tk.StringVar(value=status)
        self.status_label = ttk.Label(self, textvariable=self.motor_status_var, font=("Arial", 12))
        self.status_label.pack(pady=4)

        # Recovery UI: show active DB and queue length, and allow opening logs folder
        self.recovery_frame = ttk.Frame(self)
        self.recovery_frame.pack(pady=2)
        self.db_status_var = tk.StringVar(value="DB: none")
        ttk.Label(self.recovery_frame, textvariable=self.db_status_var).pack(side=tk.LEFT, padx=6)
        self.db_queue_var = tk.StringVar(value="Queue: 0")
        ttk.Label(self.recovery_frame, textvariable=self.db_queue_var).pack(side=tk.LEFT, padx=6)
        ttk.Button(self.recovery_frame, text="Open Logs", command=lambda: os.startfile(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs'))).pack(side=tk.LEFT, padx=6)

        # Motor controls (both)
        self.mbtn_frame = ttk.Frame(self); self.mbtn_frame.pack(pady=4, fill=tk.X, padx=16)
        self.btn_enable_motor = ttk.Button(self.mbtn_frame, text="Enable Motor", command=self.enable_motor)
        self.btn_enable_motor.grid(row=0, column=0, padx=4)
        self.btn_disable_motor = ttk.Button(self.mbtn_frame, text="Disable Motor", command=self.disable_motor)
        self.btn_disable_motor.grid(row=0, column=1, padx=4)  # DEV-only
        self.btn_jog_fwd = ttk.Button(self.mbtn_frame, text="Jog Forward", command=self.jog_forward)
        self.btn_jog_fwd.grid(row=1, column=0, padx=4)
        self.btn_jog_bwd = ttk.Button(self.mbtn_frame, text="Jog Backward", command=self.jog_backward)
        self.btn_jog_bwd.grid(row=1, column=1, padx=4)
        self.btn_clear_errors = ttk.Button(self.mbtn_frame, text="Clear Errors", command=self.clear_errors)
        self.btn_clear_errors.grid(row=2, column=0, columnspan=2, pady=4)
        self.btn_stop_motor = ttk.Button(self, text="Stop Motor", command=self.stop_motor)
        self.btn_stop_motor.pack(pady=6)

        # Track DEV-only widgets for easier hide/show
        self._dev_frames = [
            self.display_frame, self.sep_main,
            self.mpc_frame, self.model_frame
        ]
        # Keep Stop DAQ visible in Simplified mode; no DEV-only packed buttons
        self._dev_buttons_pack = []          # pack manager
        self._dev_buttons_grid = []     # grid manager (leave motor enable/disable visible in Simplified)
        # Track simplified-only widgets to show/hide with mode
        self._simplified_frames = [self.simple_mpc_frame]

    # Mode switching logic (NEW)
    def _apply_mode(self):
        mode = self.mode_var.get()
        if mode == "Simplified":
            # Hide DEV frames
            for f in self._dev_frames:
                f.pack_forget()
            # Hide DEV-only buttons
            for b in self._dev_buttons_pack:
                b.pack_forget()
            for b in self._dev_buttons_grid:
                b.grid_remove()
            # Ensure essential simplified widgets are visible
            if not self.live_label.winfo_ismapped():
                self.live_label.pack(pady=6)
            if not self.daq_frame.winfo_ismapped():
                self.daq_frame.pack(pady=4, fill=tk.X, padx=16)
            if not self.graph_frame.winfo_ismapped():
                self.graph_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)                
            if not self.spd_frame.winfo_ismapped():
                self.spd_frame.pack(pady=4, fill=tk.X, padx=16)
            # show simplified MPC controls
            if not self.simple_mpc_frame.winfo_ismapped():
                self.simple_mpc_frame.pack(pady=4, fill=tk.X, padx=16)
            if not self.status_label.winfo_ismapped():
                self.status_label.pack(pady=4)
            if not self.mbtn_frame.winfo_ismapped():
                self.mbtn_frame.pack(pady=4, fill=tk.X, padx=16)
            if not self.btn_stop_motor.winfo_ismapped():
                self.btn_stop_motor.pack(pady=6)
            self.motor_status_var.set("Mode: Simplified")
        else:
            # Show everything in original order
            if not self.display_frame.winfo_ismapped():
                self.display_frame.pack(pady=6, fill=tk.X, padx=16)
            if not self.daq_frame.winfo_ismapped():
                self.daq_frame.pack(pady=4, fill=tk.X, padx=16)
            if not self.btn_stop_daq.winfo_ismapped():
                self.btn_stop_daq.pack(side=tk.RIGHT, expand=True, padx=4)
            if not self.sep_main.winfo_ismapped():
                self.sep_main.pack(fill="x", pady=6)
            if not self.mpc_frame.winfo_ismapped():
                self.mpc_frame.pack(pady=6, fill=tk.X, padx=16)
            # hide simplified-only frame
            try:
                if self.simple_mpc_frame.winfo_ismapped():
                    self.simple_mpc_frame.pack_forget()
            except Exception:
                pass
            if not self.model_frame.winfo_ismapped():
                self.model_frame.pack(pady=6, fill=tk.X, padx=16)
            if not self.graph_frame.winfo_ismapped():
                self.graph_frame.pack(fill=tk.BOTH, expand=True, padx=16, pady=8)
            try:
                if not self.btn_disable_motor.winfo_ismapped():
                    self.btn_disable_motor.grid(row=0, column=1, padx=4)
            except Exception:
                pass
            self.motor_status_var.set("Mode: Developer")

    def _toggle_mode(self):
        self.mode_var.set("Developer" if self.mode_var.get()=="Simplified" else "Simplified")
        self._apply_mode()

    # --- NEW: Cyclic helpers ---
    def _on_cyclic_toggle(self):
        if self.cyclic_var.get():
            if self.ramp_var.get():
                self.ramp_var.set(False)
                self._on_ramp_toggle()
            a = float(self.cyclic_a_var.get())
            b = float(self.cyclic_b_var.get())
            if abs(a - b) < 1e-12:
                self.cyclic_var.set(False)
                self._cyclic_target = None
                self._cyclic_side = None
                self.motor_status_var.set("Cyclic: A and B are equal; disabled.")
                return
            # Arm cyclic at A
            self._cyclic_side = 'A'
            self._cyclic_target = a
            # reflect in setpoint field without thread issues
            self.after(0, lambda: self.torque_setpoint_var.set(a))
            if not self.mpc_enable.get():
                self.motor_status_var.set(
                    f"Cyclic armed at A={a:.2f} lbf (B={b:.2f} lbf). Enable MPC to execute.")
            else:
                self.motor_status_var.set(
                    f"Cyclic ON: targeting A={a:.2f} lbf (then B={b:.2f} lbf).")
        else:
            self._cyclic_target = None
            self._cyclic_side = None
            self.motor_status_var.set("Cyclic: OFF")

    # --- NEW: Ramp helpers ---
    def _on_ramp_toggle(self):
        if self.ramp_var.get():
            if self.cyclic_var.get():
                self.cyclic_var.set(False)
                self._on_cyclic_toggle()
            self._ramp_start_time = None
            self._ramp_start_load = None
            self._ramp_target = float(self.ramp_target_var.get())
            self._ramp_rate = None
            if not self.mpc_enable.get():
                self.motor_status_var.set(
                    f"Ramp armed to {self._ramp_target:.2f} lbf in {float(self.ramp_time_var.get()):.2f} min. Enable MPC to execute.")
            else:
                self.motor_status_var.set(
                    f"Ramp ON: target {self._ramp_target:.2f} lbf over {float(self.ramp_time_var.get()):.2f} min.")
        else:
            self._ramp_start_time = None
            self._ramp_start_load = None
            self._ramp_target = None
            self._ramp_rate = None
            self.motor_status_var.set("Ramp: OFF")

    def _apply_ramp(self, lbf_raw, t):
        # Use thread-safe snapshots; never call tk .get() from background thread
        if not (getattr(self, '_last_ramp', False) and getattr(self, '_last_mpc_enable', False)):
            return None
        if self._ramp_start_time is None:
            self._ramp_start_time = t
            self._ramp_start_load = lbf_raw
            target = float(getattr(self, '_last_ramp_target', 0.0))
            duration = max(float(getattr(self, '_last_ramp_time', 0.0)) * 60.0, 1e-9)
            self._ramp_target = target
            self._ramp_rate = (target - self._ramp_start_load) / duration
        elapsed = t - self._ramp_start_time
        next_sp = self._ramp_start_load + self._ramp_rate * elapsed
        done = False
        if (self._ramp_rate >= 0 and next_sp >= self._ramp_target) or (self._ramp_rate < 0 and next_sp <= self._ramp_target):
            next_sp = self._ramp_target
            done = True
        self.after(0, lambda: self.torque_setpoint_var.set(next_sp))
        if done:
            # Only touch Tk from main thread
            self.after(0, lambda: self.ramp_var.set(False))
            msg = f"Ramp complete at {self._ramp_target:.2f} lbf"
            self.after(0, self._on_ramp_toggle)
            self.after(0, lambda: self.motor_status_var.set(msg))
        return next_sp

    # --- DAQ loop ---
    def start_reading(self):
        if self.reading:
            return
        self.daq = DaqStream(BOARD_NUM)
        cfg = DaqConfig(channels=[CHANNEL], fs=1.0 / SAMPLE_INTERVAL, ul_range=VOLTAGE_RANGE)
        self.daq.start(cfg)
        self.log_start_time = self.daq.t0
        self.graph_start_time = self.daq.t0
        # clear in-memory buffers
        self.log_time.clear(); self.log_voltage.clear()
        self.log_mass_kg.clear(); self.log_force_lbf.clear()
        self.lbf_history.clear(); self.voltage_history.clear(); self.time_history.clear()

        # Prepare persistent DB for this run
        logs_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
        os.makedirs(logs_dir, exist_ok=True)
        ts = time.strftime('%Y%m%d_%H%M%S')
        db_fname = f'readings_{ts}.db'
        self._db_path = os.path.join(logs_dir, db_fname)
        self._start_db_writer(self._db_path)
        self.reading = True
        self.read_thread = threading.Thread(target=self._loop, daemon=True)
        self.read_thread.start()
        self._schedule_plot()

    def stop_reading(self):
        self.reading = False
        if self.read_thread is not None:
            self.read_thread.join(timeout=1.0)
            self.read_thread = None
        if hasattr(self, "daq"):
            self.daq.stop()
        # Stop DB writer (flush remaining rows) before prompting save
        self._stop_db_writer()
        self._prompt_save_data()
        # Clear the graph so a new run starts fresh
        self.lbf_history.clear()
        self.voltage_history.clear()
        self.time_history.clear()
        self.ax.clear()
        self.canvas.draw()

    def _loop(self):
        pythoncom.CoInitialize()
        voltage_buffer = []
        last_t = self.daq.t0

        while self.reading:
            block = self.daq.read()
            if block is None:
                time.sleep(0.01)
                continue
            ts, data = block
            for t, volts in zip(ts, data[:, 0]):
                try:
                    dt = t - last_t
                    last_t = t
                    if dt > WATCHDOG_DT_MAX:
                        self.after(0, self._watchdog_trip, dt)
                        continue

                    voltage_buffer.append(volts)
                    if len(voltage_buffer) > ROLLING_PERIOD:
                        voltage_buffer.pop(0)
                    avg_volts = sum(voltage_buffer) / len(voltage_buffer)

                    lbf_raw = volts_to_force_lbf(volts)
                    lbf_disp = volts_to_force_lbf(avg_volts)  # UI only

                    # also keep bounded in-memory buffers for plotting
                    with self._hist_lock:
                        self.voltage_history.append(avg_volts)
                        self.lbf_history.append(lbf_disp)
                        self.time_history.append(t - self.graph_start_time)

                    if self.log_start_time is not None:
                        # keep run-level logs
                        self.log_time.append(t - self.log_start_time)
                        self.log_voltage.append(volts)
                        self.log_force_lbf.append(lbf_disp)
                        self.log_mass_kg.append(lbf_disp / 2.20462)

                        # enqueue to persistent DB after values computed
                        try:
                            self._db_queue.put_nowait((t, float(volts), float(lbf_disp), float(lbf_disp/2.20462), float(self.current_rpm)))
                        except Exception:
                            # queue full or stopped; surface status but keep running
                            try:
                                self.after(0, self.motor_status_var.set, "DB queue error")
                            except Exception:
                                pass

                    # update UI label (always display pounds-force)
                    self.after(0, self.label_var.set, f"{lbf_disp:.2f} lbf")
                except Exception as ex_sample:
                    # Per-sample exception should not stop reading; report and continue
                    try:
                        self.after(0, self.motor_status_var.set, f"Sample error: {ex_sample}")
                    except Exception:
                        pass
                    continue

                # mode = self.read_mode_var.get()
                # if mode == "Pounds-Force":
                #     self.after(0, self.label_var.set, f"{lbf_disp:.2f} lbf")
                # elif mode == "Volts":
                #     self.after(0, self.label_var.set, f"Voltage: {avg_volts:.3f} V")
                # elif mode == "Kilograms":
                #     self.after(0, self.label_var.set, f"{lbf_disp*0.453592:.2f} kg")
                # else:  # Newtons
                #     self.after(0, self.label_var.set, f"{lbf_disp*4.448:.2f} N")


                # Always display pounds-force
                self.after(0, self.label_var.set, f"{lbf_disp:.2f} lbf")
                
                
                
                if self.log_start_time is not None:
                    self.log_force_lbf.append(lbf_disp)
                    self.log_mass_kg.append(lbf_disp / 2.20462)

                # Read the thread-safe snapshot instead of calling tk.get() here
                r_cmd = getattr(self, '_last_torque_setpoint', 0.0)
                ramp_cmd = self._apply_ramp(lbf_raw, t)
                if ramp_cmd is not None:
                    r_cmd = ramp_cmd
                elif getattr(self, '_last_cyclic', False) and getattr(self, '_last_mpc_enable', False):
                    # maintain an EMA of load slope for safe switching
                    if 'dt' in locals() and dt > 1e-9:
                        if self._cyclic_slope_ema is None:
                            self._cyclic_slope_ema = 0.0
                        inst_slope = (lbf_raw - (self._prev_lbf if hasattr(self, '_prev_lbf') and self._prev_lbf is not None else lbf_raw)) / dt
                        self._cyclic_slope_ema = 0.8*self._cyclic_slope_ema + 0.2*inst_slope
                    self._prev_lbf = lbf_raw
                    if self._cyclic_target is None or self._cyclic_side not in ('A','B'):
                        self._cyclic_side = 'A'
                        self._cyclic_target = float(getattr(self, '_last_cyclic_a', self.cyclic_a_var.get()))
                        self.after(0, lambda: self.torque_setpoint_var.set(self._cyclic_target))
                    tol = abs(float(getattr(self, '_last_cyclic_tol', self.cyclic_tol_var.get())))
                    if self._cyclic_side == 'A':
                        a = float(getattr(self, '_last_cyclic_a', self.cyclic_a_var.get()))
                        dwell_ok = (self._cyclic_last_switch_t is None) or ((t - self._cyclic_last_switch_t) >= self._cyclic_dwell_s)
                        slope_ok = (self._cyclic_slope_ema is None) or (abs(self._cyclic_slope_ema) <= self._cyclic_slope_limit)
                        if abs(lbf_raw - a) <= tol and dwell_ok and slope_ok:
                            self._cyclic_side = 'B'
                            self._cyclic_target = float(getattr(self, '_last_cyclic_b', self.cyclic_b_var.get()))
                            self._cyclic_last_switch_t = t
                            self.after(0, lambda: self.torque_setpoint_var.set(self._cyclic_target))
                            self.after(0, lambda: self.motor_status_var.set(
                                f"Cyclic: reached A≈{a:.2f} lbf → switching to B={self._cyclic_target:.2f} lbf"))
                        else:
                            self._cyclic_target = a
                    else:
                        b = float(getattr(self, '_last_cyclic_b', self.cyclic_b_var.get()))
                        dwell_ok = (self._cyclic_last_switch_t is None) or ((t - self._cyclic_last_switch_t) >= self._cyclic_dwell_s)
                        slope_ok = (self._cyclic_slope_ema is None) or (abs(self._cyclic_slope_ema) <= self._cyclic_slope_limit)
                        if abs(lbf_raw - b) <= tol and dwell_ok and slope_ok:
                            self._cyclic_side = 'A'
                            self._cyclic_target = float(getattr(self, '_last_cyclic_a', self.cyclic_a_var.get()))
                            self._cyclic_last_switch_t = t
                            self.after(0, lambda: self.torque_setpoint_var.set(self._cyclic_target))
                            self.after(0, lambda: self.motor_status_var.set(
                                f"Cyclic: reached B≈{b:.2f} lbf → switching to A={self._cyclic_target:.2f} lbf"))
                        else:
                            self._cyclic_target = b
                    r_cmd = self._cyclic_target

                # if self.mnt_ctrl and self.mpc_enable.get():
                #     self.mpc.set_weights(self.Np_var.get(), self.Nu_var.get(),
                #                          self.qy_var.get(), self.rdu_var.get())
                #     if self.adapt_var.get():
                #         A_new, B_new = self.rls.update(lbf_raw, self.current_rpm)
                #         if int(time.time()*10) % 10 == 0:
                #             self.mpc.set_model(SAMPLE_INTERVAL, A_new, B_new, self.mpc.nk)
                #             self.model_lbl.set(self._model_str(A_new, B_new, self.mpc.nk))
                #     u_cmd = self.mpc.step(y_meas=lbf_raw,
                #                           r=r_cmd,
                #                           u_prev=self.current_rpm)
                #     self.after(0, self._send_motor_command, u_cmd)

                # if self._id_running:
                #     self._id_y.append(lbf_raw)
                #     self._id_t.append(self._id_t[-1] + SAMPLE_INTERVAL if self._id_t else 0.0)
                # in _loop, where you currently call self.mpc.step(...)
                try:
                    if self.mnt_ctrl and getattr(self, '_last_mpc_enable', False):
                        # Use snapshots for MPC params
                        self.mpc.set_weights(int(getattr(self, '_last_Np', self.mpc.Np)),
                                             int(getattr(self, '_last_Nu', self.mpc.Nu)),
                                             float(getattr(self, '_last_qy', self.mpc.qy)),
                                             float(getattr(self, '_last_rdu', self.mpc.rdu)))
                        if getattr(self, '_last_adapt', False):
                            A_new, B_new = self.rls.update(lbf_raw, self.current_rpm)
                            if int(time.time()*10) % 10 == 0:
                                self.mpc.set_model(SAMPLE_INTERVAL, A_new, B_new, self.mpc.nk)
                                # UI update must occur on main thread
                                self.after(0, lambda: self.model_lbl.set(self._model_str(A_new, B_new, self.mpc.nk)))

                        u_cmd = self.mpc.step(y_meas=lbf_raw, r=r_cmd, u_prev=self.current_rpm)
                        self.after(0, self._send_motor_command, u_cmd)
                except Exception as e:
                    # Keep logging running; just turn MPC off and surface the error
                    self.after(0, self.motor_status_var.set, f"MPC disabled this tick: {e}")
                    self.after(0, lambda: self.mpc_enable.set(False))
                    # continue the loop normally


        pythoncom.CoUninitialize()

    # --- plotting ---
    def _schedule_plot(self):
        if not self.reading: return
        self.ax.clear()
        # Snapshot histories under a lock to avoid length mismatch during concurrent updates
        try:
            with self._hist_lock:
                tlist = list(self.time_history)
                ylist = list(self.lbf_history)
        except Exception:
            tlist, ylist = [], []

        n = min(len(tlist), len(ylist))
        if n > 0:
            self.ax.plot(tlist[:n], ylist[:n], lw=2)
        self.ax.set_xlabel('Time (s)')
        self.ax.set_ylabel('Force (lbf)')
        self.ax.set_title('Live Force')
        self.canvas.draw()
        self.after(self.update_graph_interval, self._schedule_plot)

    def reset_graph(self):
        """Visually reset the live graph without touching persistent logs."""
        try:
            # Clear in-memory plot buffers only
            with self._hist_lock:
                self.lbf_history.clear()
                self.voltage_history.clear()
                self.time_history.clear()
            # Reset graph start time so x-axis restarts from zero relative to now
            self.graph_start_time = time.time()
            # Redraw empty graph
            self.ax.clear()
            self.ax.set_title('Live Force / Voltage')
            self.ax.set_xlabel('Time (s)')
            self.ax.set_ylabel('Value')
            self.canvas.draw()
            self.motor_status_var.set('Graph reset (visual only)')
        except Exception as e:
            try:
                self.motor_status_var.set(f'Graph reset failed: {e}')
            except Exception:
                pass

    # --- Safety / motor ---
    def _send_motor_command(self, desired_rpm):
        safe_rpm = self._safety_filter(desired_rpm)
        try:
            self.mnt_ctrl.SetJog(AXIS_ID, safe_rpm / 6.0)  # 6 steps / rev at 36°/step
            self.current_rpm = safe_rpm
            if self._id_running:
                self._id_u.append(self.current_rpm)
            self.motor_status_var.set(f"Motor: Command {safe_rpm:.2f} RPM")
        except Exception as e:
            self.motor_status_var.set(f"Jog error: {e}")

    def _safety_filter(self, desired_rpm):
        desired = max(min(desired_rpm, SAFETY_MAX_RPM), -SAFETY_MAX_RPM)
        now = time.time()
        if self._last_cmd_time is None:
            allowed = abs(desired)
        else:
            allowed = SLEW_RATE_RPM_S * (now - self._last_cmd_time)
        lower, upper = self._last_cmd_rpm - allowed, self._last_cmd_rpm + allowed
        ramped = max(min(desired, upper), lower)
        if abs(ramped) < COMMAND_DEADBAND_RPM:
            ramped = 0.0
        self._last_cmd_time = now
        self._last_cmd_rpm = ramped
        return ramped

    def _watchdog_trip(self, dt):
        self.motor_status_var.set(f"Watchdog: dt={dt:.3f}s → safe ramp + reset")
        self._ramp_to_zero_then_reset()

    def _ramp_to_zero_then_reset(self):
        def step():
            if abs(self._last_cmd_rpm) <= COMMAND_DEADBAND_RPM + 1e-6:
                self._send_motor_command(0.0)
                self.mpc.reset()
                return
            self._send_motor_command(0.0)
            self.after(int(SAMPLE_INTERVAL*1000), step)
        step()

    # --- Manual motor controls ---
    def _update_speed_display(self, *args):
        self.speed_display_var.set(f"Jog Speed: {self.speed_var.get():.1f} {self.unit_var.get()}")

    def enable_motor(self):
        if not self.mnt_ctrl:
            self.motor_status_var.set(self.motor_error_msg); return
        try:
            self.mnt_ctrl.SetDriveEnable(AXIS_ID, True)
            self.motor_status_var.set("Motor: Enabled")
        except Exception as e:
            self.motor_status_var.set(f"Error: {e}")

    def disable_motor(self):
        if not self.mnt_ctrl:
            self.motor_status_var.set(self.motor_error_msg); return
        try:
            # Turning off drive disables cyclic for safety
            if self.cyclic_var.get():
                self.cyclic_var.set(False)
                self._on_cyclic_toggle()
            if self.ramp_var.get():
                self.ramp_var.set(False)
                self._on_ramp_toggle()
            self._ramp_to_zero_then_reset()
            self.mnt_ctrl.SetDriveEnable(AXIS_ID, False)
            self.motor_status_var.set("Motor: Disabled")
        except Exception as e:
            self.motor_status_var.set(f"Error: {e}")

    def jog_forward(self):
        if not self.mnt_ctrl:
            self.motor_status_var.set(self.motor_error_msg); return
        rpm = self.speed_var.get() if self.unit_var.get()=="RPM" else self.speed_var.get()*6.0
        self._send_motor_command(abs(rpm))

    def jog_backward(self):
        if not self.mnt_ctrl:
            self.motor_status_var.set(self.motor_error_msg); return
        rpm = self.speed_var.get() if self.unit_var.get()=="RPM" else self.speed_var.get()*6.0
        self._send_motor_command(-abs(rpm))

    def stop_motor(self):
        if not self.mnt_ctrl:
            self.motor_status_var.set(self.motor_error_msg); return
        try:
            # Stopping disables cyclic per your spec
            if self.cyclic_var.get():
                self.cyclic_var.set(False)
                self._on_cyclic_toggle()
            if self.ramp_var.get():
                self.ramp_var.set(False)
                self._on_ramp_toggle()
            self._ramp_to_zero_then_reset()
            self.mnt_ctrl.DoStop(AXIS_ID)
            self.motor_status_var.set("Motor: Stopped")
        except Exception as e:
            self.motor_status_var.set(f"Error: {e}")

    def clear_errors(self):
        if not self.mnt_ctrl:
            self.motor_status_var.set(self.motor_error_msg); return
        try:
            self.mnt_ctrl.DoErrorClear(0, 0)
            self.motor_status_var.set("Motor: Errors Cleared")
        except Exception as e:
            self.motor_status_var.set(f"Error: {e}")

    # --- ARX ID (LS, no deps) ---
    def _start_arx_id(self):
        if self._id_running: return
        if not self.mnt_ctrl:
            self.motor_status_var.set("ID aborted: motor link not ready")
            return
        # reset
        self._id_u.clear(); self._id_y.clear(); self._id_t.clear()
        self._id_dt = SAMPLE_INTERVAL
        self._id_running = True
        self.motor_status_var.set("ID: PRBS excitation…")
        dur = 30.0; amp = 6.0  # defaults; adjust in code if desired
        self._run_prbs(dur, amp)

    def _run_prbs(self, dur_s=30.0, amp_rpm=6.0):
        start = time.time()
        flip_hz = 0.6
        next_flip = start
        target = 0.0

        def tick():
            now = time.time()
            if (now - start) >= dur_s or not self._id_running:
                self._send_motor_command(0.0)
                self.after(50, self._finish_arx_id)
                return
            nonlocal next_flip, target
            if now >= next_flip:
                bit = 1 if random.random()>0.5 else -1
                target = bit * amp_rpm
                next_flip = now + (1.0/flip_hz)*(0.6 + 0.8*random.random())
            self._send_motor_command(target)
            self.after(int(SAMPLE_INTERVAL*1000), tick)
        tick()

    def _finish_arx_id(self):
        self._id_running = False
        n = min(len(self._id_u), len(self._id_y))
        u = np.asarray(self._id_u[:n], dtype=float)
        y = np.asarray(self._id_y[:n], dtype=float)
        # Demean for LS stability (keep signs)
        u -= np.mean(u); y -= np.mean(y)
        try:
            model = self._identify_arx_ls(u, y, dt=SAMPLE_INTERVAL)
        except Exception as e:
            self.motor_status_var.set(f"ID failed: {e}")
            return
        # Apply model
        self.mpc.set_model(model["dt"], model["A"], model["B"], model["nk"])
        self.rls = RLS_ARX(na=len(model["A"])-1, nb=len(model["B"]), nk=model["nk"], lam=0.995)
        self.model_lbl.set(self._model_str())
        # Popup + save
        self._show_text_popup("ARX Identification Result", self._format_arx_summary(model))
        with open("arx_model.json","w") as f: json.dump(model, f, indent=2)
        self.motor_status_var.set("ID done. Model applied + saved to arx_model.json")

    def _identify_arx_ls(self, u, y, dt):
        # grid (slightly wider) and one-step AIC
        na_grid, nb_grid, nk_grid = [1,2,3], [1,2,3], [0,1,2,3]
        best = None
        N = len(y)
        for na in na_grid:
            for nb in nb_grid:
                for nk in nk_grid:
                    try:
                        A, B, resid = self._fit_arx_one_step(u, y, na, nb, nk)
                        kparams = na + nb
                        aic = N*np.log(np.var(resid) + 1e-12) + 2*kparams
                        if (best is None) or (aic < best[0]):
                            best = (aic, na, nb, nk, A, B, resid)
                    except Exception:
                        continue
        if best is None:
            raise RuntimeError("No ARX model could be fitted.")
        _, na, nb, nk, A, B, resid = best
        out = {"structure":"ARX","A":A.tolist(),"B":B.tolist(),"nk":nk,"dt":float(dt)}
        fitR2 = 1.0 - np.var(resid)/(np.var(y)+1e-12)
        out["fit_R2"] = float(max(0.0, min(1.0, fitR2)))
        if ct is not None:
            num = (np.concatenate((np.zeros(nk), B))).tolist()
            den = A.tolist()
            out["tf_discrete_num"] = num; out["tf_discrete_den"] = den
        return out

    def _fit_arx_one_step(self, u, y, na, nb, nk):
        # Build regressor for k = maxlag..N-1
        N = len(y)
        maxlag = max(na, nb+nk)
        Phi, Y = [], []
        for k in range(maxlag, N):
            row = []
            for i in range(1, na+1):
                row.append(-y[k-i])
            for j in range(1, nb+1):
                row.append(u[k-(nk+j)])
            Phi.append(row); Y.append(y[k])
        Phi = np.asarray(Phi); Y = np.asarray(Y)
        theta, *_ = np.linalg.lstsq(Phi, Y, rcond=None)
        a = theta[:na]; b = theta[na:]
        A = np.concatenate(([1.0], a)); B = b.copy()
        # one-step prediction residuals
        yhat = Phi @ theta
        resid = Y - yhat
        return A, B, resid

    # --- Model JSON helpers ---
    def _load_model_json(self):
        fname = filedialog.askopenfilename(filetypes=[("JSON","*.json"),("All files","*.*")])
        if not fname: return
        with open(fname,"r") as f:
            m = json.load(f)
        self.mpc.set_model(m.get("dt", DEFAULT_DT), m["A"], m["B"], m.get("nk", 0))
        self.rls = RLS_ARX(na=len(m["A"])-1, nb=len(m["B"]), nk=m.get("nk",0), lam=0.995)
        self.model_lbl.set(self._model_str())
        self.motor_status_var.set("Model loaded.")

    def _save_model_json(self):
        m = {"A": self.mpc.A.tolist(),
             "B": self.mpc.B.tolist(),
             "nk": self.mpc.nk, "dt": self.mpc.dt}
        m["note"] = "ARX model used by MPC"
        fname = filedialog.asksaveasfilename(defaultextension=".json",
                    filetypes=[("JSON","*.json"),("All files","*.*")])
        if not fname: return
        with open(fname,"w") as f: json.dump(m, f, indent=2)
        self.motor_status_var.set("Model saved.")

    def _load_default_model(self):
        """Load model from tests/smooth_ramp.json if present (silent on failure)."""
        # locate file relative to script/workspace
        base_dir = os.path.dirname(os.path.abspath(__file__))
        default_path = os.path.join(base_dir, 'tests', 'smooth_ramp.json')
        if not os.path.isfile(default_path):
            return
        with open(default_path, 'r') as f:
            try:
                m = json.load(f)
            except Exception:
                return
        # apply to MPC and RLS
        try:
            self.mpc.set_model(m.get('dt', DEFAULT_DT), m['A'], m['B'], m.get('nk', 0))
            self.rls = RLS_ARX(na=len(m['A'])-1, nb=len(m['B']), nk=m.get('nk', 0), lam=0.995)
            # update UI strings
            try:
                self.model_lbl.set(self._model_str())
            except Exception:
                pass
            self.motor_status_var.set('Default model loaded from tests/smooth_ramp.json')
        except Exception:
            return

    def _reset_model(self):
        self.mpc.set_model(DEFAULT_DT, DEFAULT_A, DEFAULT_B, DEFAULT_NK)
        self.rls = RLS_ARX(na=len(DEFAULT_A)-1, nb=len(DEFAULT_B), nk=DEFAULT_NK, lam=0.995)
        self.model_lbl.set(self._model_str())
        self.motor_status_var.set("Model reset to defaults.")

    def _model_str(self, A=None, B=None, nk=None):
        A = A if A is not None else self.mpc.A
        B = B if B is not None else self.mpc.B
        nk = nk if nk is not None else self.mpc.nk
        return f"A: {np.round(A,6).tolist()}   B: {np.round(B,6).tolist()}   nk={nk}, dt={self.mpc.dt:.3f}s"

    # --- Save & Close ---
    def _prompt_save_data(self):
        # Export logged data. Prefer exporting from persistent DB if available.
        try:
            import pandas as pd
        except Exception:
            self.motor_status_var.set('Pandas not installed: cannot export data')
            return

        fname = filedialog.asksaveasfilename(defaultextension=".xlsx",
                    filetypes=[("Excel files","*.xlsx"),("All files","*.*")])
        if not fname:
            return

        # If DB exists, gather from all rotated DB parts in the logs folder
        if self._db_path:
            try:
                base_dir = os.path.dirname(self._db_path)
                base_name = os.path.splitext(os.path.basename(self._db_path))[0]
                # gather files: base + .partN.db
                candidates = []
                base_file = os.path.join(base_dir, f"{base_name}.db")
                if os.path.isfile(base_file):
                    candidates.append(base_file)
                # check for part files
                for i in range(1, self._db_rotate_count + 1):
                    p = os.path.join(base_dir, f"{base_name}.part{i}.db")
                    if os.path.isfile(p):
                        candidates.append(p)

                if candidates:
                    dfs = []
                    for p in candidates:
                        try:
                            con = sqlite3.connect(p)
                            dfp = pd.read_sql_query('SELECT timestamp AS "Time (s)", voltage AS "Voltage (V)", force_lbf AS "Force (lbf)", mass_kg AS "Torque (kg)" FROM readings ORDER BY id', con)
                            con.close()
                            dfs.append(dfp)
                        except Exception:
                            continue
                    if dfs:
                        df = pd.concat(dfs, ignore_index=True)
                        df.to_excel(fname, index=False)
                        self.motor_status_var.set(f'Data exported to {os.path.basename(fname)}')
                        return
            except Exception:
                pass

        # If no DB, export from in-memory buffers
        try:
            df = pd.DataFrame({
                "Time (s)": list(self.log_time),
                "Voltage (V)": list(self.log_voltage),
                "Force (lbf)": list(self.log_force_lbf),
                "Torque (kg)": list(self.log_mass_kg),
            })
            df.to_excel(fname, index=False)
            self.motor_status_var.set(f'Data exported to {os.path.basename(fname)}')
        except Exception:
            self.motor_status_var.set('Export failed')

    # --- Persistent DB writer management ---
    def _start_db_writer(self, db_path):
        try:
            # Open DB and create table
            conn = sqlite3.connect(db_path, check_same_thread=False)
            cur = conn.cursor()
            cur.execute('''CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                voltage REAL,
                force_lbf REAL,
                mass_kg REAL,
                rpm REAL
            )''')
            conn.commit()
            self._db_conn = conn
            self._db_path = db_path
            self._db_rotate_count = 0

            # Start writer thread
            self._db_stop_event.clear()
            t = threading.Thread(target=self._db_writer_loop, daemon=True)
            self._db_writer_thread = t
            t.start()
        except Exception as e:
            self.motor_status_var.set(f'DB writer start failed: {e}')

    def _stop_db_writer(self):
        # Signal stop and wait for queue to flush
        try:
            if self._db_writer_thread is None:
                return
            self._db_stop_event.set()
            # wait until queue empty or timeout
            start = time.time()
            while not self._db_queue.empty() and (time.time() - start) < 5.0:
                time.sleep(0.05)
            # join thread
            self._db_writer_thread.join(timeout=2.0)
            if self._db_conn:
                try:
                    self._db_conn.commit()
                    self._db_conn.close()
                except Exception:
                    pass
            self._db_writer_thread = None
            self._db_conn = None
        except Exception:
            pass

    def _db_writer_loop(self):
        conn = self._db_conn
        cur = conn.cursor()
        while not self._db_stop_event.is_set() or not self._db_queue.empty():
            try:
                item = self._db_queue.get(timeout=0.2)
            except Exception:
                continue
            try:
                ts, volts, force_lbf, mass_kg, rpm = item
                cur.execute('INSERT INTO readings (timestamp, voltage, force_lbf, mass_kg, rpm) VALUES (?, ?, ?, ?, ?)',
                            (ts, volts, force_lbf, mass_kg, rpm))
            except Exception:
                pass
            # periodic commit for durability
            if int(time.time()) % 2 == 0:
                try:
                    conn.commit()
                except Exception:
                    pass
            # Check rotation by file size
            try:
                if self._db_path and os.path.isfile(self._db_path):
                    if os.path.getsize(self._db_path) > self._db_max_bytes:
                        # rotate DB: close current and open a new one with suffix
                        try:
                            conn.commit()
                        except Exception:
                            pass
                        try:
                            conn.close()
                        except Exception:
                            pass
                        self._db_rotate_count += 1
                        base_dir = os.path.dirname(self._db_path)
                        base_name = os.path.splitext(os.path.basename(self._db_path))[0]
                        new_name = f"{base_name}.part{self._db_rotate_count}.db"
                        new_path = os.path.join(base_dir, new_name)
                        # create new DB and switch
                        try:
                            conn = sqlite3.connect(new_path, check_same_thread=False)
                            cur = conn.cursor()
                            cur.execute('''CREATE TABLE IF NOT EXISTS readings (
                                id INTEGER PRIMARY KEY AUTOINCREMENT,
                                timestamp REAL,
                                voltage REAL,
                                force_lbf REAL,
                                mass_kg REAL,
                                rpm REAL
                            )''')
                            conn.commit()
                            self._db_conn = conn
                            self._db_path = new_path
                        except Exception as e:
                            # if rotation failed, try to reopen old path
                            try:
                                conn = sqlite3.connect(self._db_path, check_same_thread=False)
                                cur = conn.cursor()
                            except Exception:
                                pass
            except Exception:
                pass
        try:
            conn.commit()
        except Exception:
            pass

    def _show_text_popup(self, title, text):
        win = tk.Toplevel(self); win.title(title)
        txt = tk.Text(win, width=90, height=28)
        txt.insert("1.0", text); txt.configure(state="disabled"); txt.pack(padx=10, pady=10)
        ttk.Button(win, text="Close", command=win.destroy).pack(pady=6)

    def _update_db_ui(self):
        # Update DB status and queue length every second
        try:
            if self._db_path:
                name = os.path.basename(self._db_path)
            else:
                name = 'none'
            self.db_status_var.set(f"DB: {name}")
            qlen = self._db_queue.qsize() if self._db_queue is not None else 0
            self.db_queue_var.set(f"Queue: {qlen}")
        except Exception:
            pass
        try:
            self.after(1000, self._update_db_ui)
        except Exception:
            pass

    def _format_arx_summary(self, model):
        lines = []
        lines.append("=== ARX Identification Result ===")
        lines.append(f"dt (s): {model['dt']:.4f}   nk: {model['nk']}   Fit (R^2): {model['fit_R2']:.3f}")
        lines.append(f"A(z^-1): {model['A']}")
        lines.append(f"B(z^-1): {model['B']}")
        if 'tf_discrete_num' in model:
            lines.append(f"num: {model['tf_discrete_num']}")
            lines.append(f"den: {model['tf_discrete_den']}")
        return "\n".join(lines)

    def on_closing(self):
        self.stop_reading()
        if self.mnt_ctrl:
            try:
                self.mnt_ctrl.SetDriveEnable(AXIS_ID, False)
                self.mnt_ctrl.SetNullLink()
            except:
                pass
        pythoncom.CoUninitialize()
        self.destroy()


if __name__ == '__main__':
    app = DAQMotorApp()
    app.mainloop()
