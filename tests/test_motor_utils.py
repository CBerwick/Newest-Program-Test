import importlib.util
import os
import sys
import types
import math


def import_gui_module():
    tk_stub = types.ModuleType("tkinter")

    class DummyWidget:
        def __init__(self, *args, **kwargs):
            pass
        def pack(self, *a, **k):
            pass
        def grid(self, *a, **k):
            pass
        def destroy(self):
            pass
        def pack_forget(self, *a, **k):
             pass
        def grid_remove(self, *a, **k):
             pass
        def winfo_ismapped(self):
             return False

    class DummyTk(DummyWidget):
        def title(self, *a):
            pass
        def geometry(self, *a):
            pass
        def protocol(self, *a, **k):
            pass
        def mainloop(self):
            pass
        def bind(self, *a, **k):
            pass
        def after(self, delay, func=None, *args):
            if func:
                func(*args)

    tk_stub.Tk = DummyTk

    class DummyVar:
        def __init__(self, value=None):
            self._value = value
        def get(self):
            return self._value
        def set(self, value):
            self._value = value
        def trace_add(self, *a, **k):
            pass

    tk_stub.StringVar = DummyVar
    tk_stub.DoubleVar = DummyVar
    tk_stub.BooleanVar = DummyVar
    tk_stub.IntVar = DummyVar
    tk_stub.X = "x"
    tk_stub.LEFT = "left"
    tk_stub.RIGHT = "right"
    tk_stub.BOTH = "both"
    tk_stub.Toplevel = lambda *a, **k: DummyWidget()
    tk_stub.Text = DummyWidget

    filedialog_stub = types.ModuleType("tkinter.filedialog")
    filedialog_stub.asksaveasfilename = lambda *a, **k: "dummy.xlsx"
    filedialog_stub.askopenfilename = lambda *a, **k: ""
    tk_stub.filedialog = filedialog_stub

    ttk_stub = types.ModuleType("tkinter.ttk")
    for name in [
        "LabelFrame",
        "Radiobutton",
        "Label",
        "Button",
        "Frame",
        "Separator",
        "Spinbox",
        "OptionMenu",
        "Checkbutton",
        "Entry",
        "Toplevel",
        "Text",
    ]:
        setattr(ttk_stub, name, lambda *a, **k: DummyWidget())
    tk_stub.ttk = ttk_stub

    sys.modules.setdefault("tkinter", tk_stub)
    sys.modules.setdefault("tkinter.ttk", ttk_stub)
    sys.modules.setdefault("tkinter.filedialog", filedialog_stub)

    win32_stub = types.ModuleType("win32com")
    dummy_motor = types.SimpleNamespace(
        SetUSBControllerLink=lambda *a, **k: None,
        DriveEnable=lambda *a, **k: False,
        SetDriveEnable=lambda *a, **k: None,
        DoStop=lambda *a, **k: None,
        SetJog=lambda *a, **k: None,
        DoErrorClear=lambda *a, **k: None,
        ErrorPresent=False,
        SetNullLink=lambda *a, **k: None,
    )
    client_sub = types.SimpleNamespace(Dispatch=lambda *a, **k: dummy_motor)
    win32_stub.client = client_sub
    sys.modules.setdefault("win32com", win32_stub)
    sys.modules.setdefault("win32com.client", client_sub)

    pythoncom_stub = types.ModuleType("pythoncom")
    pythoncom_stub.CoInitialize = lambda *a, **k: None
    pythoncom_stub.CoUninitialize = lambda *a, **k: None
    sys.modules.setdefault("pythoncom", pythoncom_stub)

    mpl_figure = types.ModuleType("matplotlib.figure")
    class DummyAxes:
        def clear(self):
            pass
        def plot(self, *a, **k):
            pass
        def set_xlabel(self, *a, **k):
            pass
        def set_ylabel(self, *a, **k):
            pass
        def set_title(self, *a, **k):
            pass
    class DummyFigure:
        def __init__(self, *a, **k):
            pass
        def subplots(self):
            return DummyAxes()
    mpl_figure.Figure = DummyFigure
    mpl_backend = types.ModuleType("matplotlib.backends.backend_tkagg")
    mpl_backend.FigureCanvasTkAgg = lambda *a, **k: types.SimpleNamespace(get_tk_widget=lambda: DummyWidget())
    sys.modules.setdefault("matplotlib", types.ModuleType("matplotlib"))
    sys.modules.setdefault("matplotlib.figure", mpl_figure)
    sys.modules.setdefault("matplotlib.backends.backend_tkagg", mpl_backend)

    mcc_stub = types.ModuleType("mcculw")
    ul_stub = types.SimpleNamespace(
        a_in_32=lambda *a, **k: 0,
        to_eng_units_32=lambda *a, **k: 0.0,
        a_input_mode=lambda *a, **k: None,
    )
    enums = types.SimpleNamespace(
        ULRange=types.SimpleNamespace(BIP10VOLTS=1),
        AnalogInputMode=types.SimpleNamespace(DIFFERENTIAL=1),
        FunctionType=types.SimpleNamespace(AIFUNCTION=1),
        ScanOptions=types.SimpleNamespace(BACKGROUND=1, CONTINUOUS=2, SCALEDATA=4),
        Status=types.SimpleNamespace(IDLE=0),
    )
    mcc_stub.ul = ul_stub
    mcc_stub.enums = enums
    sys.modules.setdefault("mcculw", mcc_stub)
    sys.modules.setdefault("mcculw.ul", ul_stub)
    sys.modules.setdefault("mcculw.enums", enums)

    spec = importlib.util.spec_from_file_location(
        "motor_gui",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "shear_test_app.py"),
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_volts_to_force_lbf_conversion():
    mod = import_gui_module()
    expected = 261.19447705 * 0.5 + 0.00785081
    assert math.isclose(mod.volts_to_force_lbf(0.5), expected, rel_tol=1e-6)


def test_mpc_arx_step_outputs_float():
    mod = import_gui_module()
    mpc = mod.MPC_ARX(dt=0.1, A=[1, -0.5], B=[0.5], nk=0, Np=5, Nu=1, qy=1.0, rdu=1.0, use_osqp=False)
    cmd = mpc.step(y_meas=0.0, r=1.0, u_prev=0.0)
    assert isinstance(cmd, float)


def test_ramp_to_target_progress():
    mod = import_gui_module()
    app = mod.DAQMotorApp()
    app.mpc_enable.set(True)
    app.ramp_target_var.set(10.0)
    app.ramp_time_var.set(0.1)  # 0.1 min = 6 s
    app.ramp_var.set(True)
    app._on_ramp_toggle()
    sp0 = app._apply_ramp(lbf_raw=0.0, t=0.0)
    assert sp0 == 0.0
    sp_half = app._apply_ramp(lbf_raw=0.0, t=3.0)
    assert math.isclose(sp_half, 5.0, rel_tol=1e-2)
    sp_full = app._apply_ramp(lbf_raw=0.0, t=6.0)
    assert math.isclose(sp_full, 10.0, rel_tol=1e-2)
    assert not app.ramp_var.get()
