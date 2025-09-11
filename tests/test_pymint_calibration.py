import importlib.util
import os
import sys
import types
from unittest import mock


def import_module_without_side_effects():
    """Import pymint_torque_calibration without running its data loop."""
    # Provide stub packages so the module can be executed
    pandas_stub = types.ModuleType("pandas")
    class DummyDF:
        def __init__(self, *args, **kwargs):
            pass
        def to_excel(self, *args, **kwargs):
            pass
    pandas_stub.DataFrame = DummyDF
    sys.modules.setdefault("pandas", pandas_stub)

    mcculw_stub = types.ModuleType("mcculw")
    ul_stub = types.SimpleNamespace(a_input_mode=lambda *a, **k: None)
    mcculw_stub.ul = ul_stub
    enums = types.SimpleNamespace(
        ULRange=types.SimpleNamespace(BIP10VOLTS=1),
        AnalogInputMode=types.SimpleNamespace(DIFFERENTIAL=1),
        FunctionType=types.SimpleNamespace(AIFUNCTION=1),
        ScanOptions=types.SimpleNamespace(BACKGROUND=1, CONTINUOUS=2, SCALEDATA=4),
        Status=types.SimpleNamespace(IDLE=0),
    )
    mcculw_stub.enums = enums
    sys.modules.setdefault("mcculw", mcculw_stub)
    sys.modules.setdefault("mcculw.ul", ul_stub)
    sys.modules.setdefault("mcculw.enums", enums)

    spec = importlib.util.spec_from_file_location(
        "pymint_torque_calibration",
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "pymint_torque_calibration.py"),
    )
    module = importlib.util.module_from_spec(spec)
    # Patch range so the calibration loop does not execute
    with mock.patch("builtins.range", lambda x: []):
        spec.loader.exec_module(module)
    return module


def test_read_torque_voltage_returns_converted_value():
    mod = import_module_without_side_effects()
    with mock.patch.object(mod.ul, "a_in_32", return_value=123, create=True) as mock_ain, \
         mock.patch.object(mod.ul, "to_eng_units_32", return_value=4.56, create=True) as mock_conv:
        result = mod.read_torque_voltage()
    assert result == 4.56
    mock_ain.assert_called_once_with(mod.BOARD_NUM, mod.TORQUE_SENSOR_CHANNEL, mod.VOLTAGE_RANGE)
    mock_conv.assert_called_once_with(mod.BOARD_NUM, mod.VOLTAGE_RANGE, 123)
