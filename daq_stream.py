"""Hardware-timed analog input streaming for MCC/ULDAQ devices.

This module provides the :class:`DaqStream` class which wraps the
continuous background scan facilities of the Measurement Computing
Universal Library (``mcculw``) or the newer ``uldaq`` package.  The
class hides the low level buffer management and exposes a small API
suitable for deterministic data pipelines.

The implementation prefers ``mcculw`` when available (typically on
Windows with InstaCal board numbers) and falls back to ``uldaq`` on
platforms where ``mcculw`` is not supported.  Both backends operate in a
similar fashion:

* Allocate a circular buffer sized for ``N`` seconds of data using the
  libraries' helper allocators.
* Start a background continuous scan paced by the device's internal
  clock using the ``a_in_scan``/``AInScan`` function.
* In a worker thread poll the scan status to discover newly written
  regions of the buffer and copy them into NumPy arrays.
* Timestamp each sample deterministically based on the initial start
  time and sample index (``t = t0 + sample_index / fs``).
* Push blocks of samples into a thread–safe queue that downstream
  consumers can drain without touching the hardware drivers directly.

This module intentionally avoids any GUI or application specific
behavior so that it can be reused by both the Tkinter application and
command line utilities.
"""
from __future__ import annotations

import ctypes
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

import numpy as np

try:  # Prefer the traditional Universal Library
    from mcculw import ul  # type: ignore
    from mcculw.enums import FunctionType, ScanOptions, Status, ULRange  # type: ignore

    _HAVE_MCCULW = True
except Exception:  # pragma: no cover - library not installed on CI
    _HAVE_MCCULW = False

try:  # Fallback to the cross platform uldaq package
    import uldaq  # type: ignore

    _HAVE_ULDAQ = True
except Exception:  # pragma: no cover - library not installed on CI
    _HAVE_ULDAQ = False


class DaqError(RuntimeError):
    """Raised when the DAQ backend encounters an unrecoverable error."""


@dataclass
class DaqConfig:
    """Configuration for :class:`DaqStream`.

    Attributes
    ----------
    channels:
        Sequence of analog input channels to scan.
    fs:
        Sample rate per channel (Hz).
    ul_range:
        Measurement range for all channels.
    secs:
        Size of the internal ring buffer in seconds.
    """

    channels: Sequence[int]
    fs: float
    ul_range: "ULRange" = ULRange.BIP10VOLTS if _HAVE_MCCULW else 0
    secs: float = 3.0


class DaqStream:
    """Continuous background analog input scan."""

    def __init__(self, board_num: int = 0, max_queue: int = 10) -> None:
        self.board_num = board_num
        self.max_queue = max_queue
        self._cfg: Optional[DaqConfig] = None
        self._memhandle: Optional[int] = None
        self._worker: Optional[threading.Thread] = None
        self._queue: "queue.Queue[Tuple[np.ndarray, np.ndarray]]" | None = None
        self._stop = threading.Event()
        self._t0 = 0.0
        self._sample_index = 0
        self._num_points = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def start(self, cfg: DaqConfig) -> float:
        """Start the hardware timed acquisition.

        Parameters
        ----------
        cfg:
            Configuration describing the channels and sample rate.

        Returns
        -------
        float
            The actual sample rate per channel configured by the driver.
        """

        if not _HAVE_MCCULW and not _HAVE_ULDAQ:
            raise DaqError("No supported DAQ library (mcculw or uldaq) is installed")

        self._cfg = cfg
        channels = list(cfg.channels)
        self._queue = __import__("queue").Queue(self.max_queue)
        self._stop.clear()
        self._sample_index = 0

        num_chans = len(channels)
        self._num_points = int(math.ceil(num_chans * cfg.fs * cfg.secs))

        if _HAVE_MCCULW:
            memhandle = ul.scaled_win_buf_alloc(self._num_points)
            if memhandle == 0:
                raise DaqError("scaled_win_buf_alloc failed")
            self._memhandle = memhandle

            low_chan = min(channels)
            high_chan = max(channels)
            options = ScanOptions.BACKGROUND | ScanOptions.CONTINUOUS | ScanOptions.SCALEDATA
            actual_fs = ul.a_in_scan(
                self.board_num,
                low_chan,
                high_chan,
                self._num_points,
                int(cfg.fs),
                cfg.ul_range,
                memhandle,
                options,
            )
            self._backend = "mcculw"
            self._t0 = time.monotonic()
            self._worker = threading.Thread(target=self._worker_mcculw, daemon=True)
            self._worker.start()
            return actual_fs

        # ULDAQ backend --------------------------------------------------
        devices = uldaq.get_daq_device_inventory(uldaq.InterfaceType.USB)
        if not devices:
            raise DaqError("No ULDAQ devices found")
        self._daq_device = uldaq.DaqDevice(devices[0])
        self._ai_dev = self._daq_device.get_ai_device()
        if self._ai_dev is None:
            raise DaqError("Device has no analog input capability")

        points_per_chan = int(math.ceil(cfg.fs * cfg.secs))
        data_ptr = uldaq.create_float_buffer(num_chans, points_per_chan)
        self._memhandle = data_ptr  # type: ignore[assignment]
        scan_opts = uldaq.ScanOption.BACKGROUND | uldaq.ScanOption.CONTINUOUS
        ai_mode = uldaq.AiInputMode.SINGLE_ENDED
        flags = uldaq.AInScanFlag.DEFAULT
        low_chan = min(channels)
        high_chan = max(channels)
        actual_fs = self._ai_dev.a_in_scan(
            low_chan,
            high_chan,
            ai_mode,
            uldaq.Range.BIP10VOLTS,
            points_per_chan,
            cfg.fs,
            scan_opts,
            flags,
            data_ptr,
        )
        self._backend = "uldaq"
        self._t0 = time.monotonic()
        self._points_per_chan = points_per_chan
        self._worker = threading.Thread(target=self._worker_uldaq, daemon=True)
        self._worker.start()
        return actual_fs

    def read(self) -> Optional[Tuple[np.ndarray, np.ndarray]]:
        """Return the newest block of samples.

        Returns
        -------
        tuple or ``None``
            ``(timestamps, data)`` where ``data`` has shape
            ``(n_samples, n_channels)``.  ``None`` if no data is available.
        """

        if self._queue is None:
            return None
        block = None
        q = self._queue
        while True:
            try:
                block = q.get_nowait()
            except __import__("queue").Empty:
                break
        return block

    def stop(self) -> None:
        """Stop the scan and free resources."""

        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None

        if self._cfg is None:
            return

        if _HAVE_MCCULW and getattr(self, "_backend", None) == "mcculw":
            try:
                ul.stop_background(self.board_num, FunctionType.AIFUNCTION)
            finally:
                if self._memhandle is not None:
                    ul.win_buf_free(self._memhandle)
                    self._memhandle = None
            return

        if _HAVE_ULDAQ and getattr(self, "_backend", None) == "uldaq":
            try:
                self._ai_dev.scan_stop()
            finally:
                if self._daq_device:
                    self._daq_device.disconnect()
                    self._daq_device.release()
            return

    # Expose start time for deterministic timestamp calculations
    @property
    def t0(self) -> float:
        """Monotonic timestamp corresponding to the first sample."""
        return self._t0

    # ------------------------------------------------------------------
    # Internal worker threads
    # ------------------------------------------------------------------
    def _worker_mcculw(self) -> None:  # pragma: no cover - requires hardware
        assert self._cfg is not None
        cfg = self._cfg
        num_chans = len(cfg.channels)
        total_points = self._num_points
        last_count = 0
        last_index = 0
        q = self._queue
        assert q is not None

        while not self._stop.is_set():
            status, cur_count, cur_index = ul.get_status(self.board_num, FunctionType.AIFUNCTION)
            if status == Status.IDLE:
                time.sleep(0.001)
                continue
            new_count = cur_count - last_count
            if new_count <= 0:
                time.sleep(0.001)
                continue
            if new_count > total_points:
                start = (cur_index + 1) % total_points
                last_count = cur_count - total_points
                new_count = total_points
            else:
                start = (last_index + 1) % total_points
                last_count = cur_count
            last_index = cur_index

            data = self._copy_mcculw(start, new_count)
            frames = data.reshape(-1, num_chans)
            t0 = self._t0 + self._sample_index / cfg.fs
            timestamps = t0 + np.arange(frames.shape[0]) / cfg.fs
            self._sample_index += frames.shape[0]

            try:
                q.put_nowait((timestamps, frames))
            except __import__("queue").Full:
                try:
                    q.get_nowait()
                except __import__("queue").Empty:
                    pass
                try:
                    q.put_nowait((timestamps, frames))
                except __import__("queue").Full:
                    pass

    def _copy_mcculw(self, start: int, count: int) -> np.ndarray:
        assert self._memhandle is not None
        total = self._num_points
        if start + count <= total:
            return self._copy_region_mcculw(start, count)
        first = self._copy_region_mcculw(start, total - start)
        second = self._copy_region_mcculw(0, count - (total - start))
        return np.concatenate((first, second))

    def _copy_region_mcculw(self, start: int, count: int) -> np.ndarray:
        assert self._memhandle is not None
        c_array = (ctypes.c_double * count)()
        ul.scaled_win_buf_to_array(self._memhandle, c_array, start, count)
        arr = np.ctypeslib.as_array(c_array)
        return arr.copy()

    def _worker_uldaq(self) -> None:  # pragma: no cover - requires hardware
        assert self._cfg is not None
        cfg = self._cfg
        num_chans = len(cfg.channels)
        total_points = self._points_per_chan * num_chans
        last_index = 0
        last_total = 0
        q = self._queue
        assert q is not None

        while not self._stop.is_set():
            status, xfer_status = self._ai_dev.get_scan_status()
            cur_index = xfer_status.current_index
            cur_total = xfer_status.current_total_count
            if status == uldaq.ScanStatus.IDLE:
                time.sleep(0.001)
                continue
            new_count = cur_total - last_total
            if new_count <= 0:
                time.sleep(0.001)
                continue
            if new_count > total_points:
                start = (cur_index + 1) % total_points
                last_total = cur_total - total_points
                new_count = total_points
            else:
                start = (last_index + 1) % total_points
                last_total = cur_total
            last_index = cur_index

            data = self._copy_uldaq(start, new_count)
            frames = data.reshape(-1, num_chans)
            t0 = self._t0 + self._sample_index / cfg.fs
            timestamps = t0 + np.arange(frames.shape[0]) / cfg.fs
            self._sample_index += frames.shape[0]

            try:
                q.put_nowait((timestamps, frames))
            except __import__("queue").Full:
                try:
                    q.get_nowait()
                except __import__("queue").Empty:
                    pass
                try:
                    q.put_nowait((timestamps, frames))
                except __import__("queue").Full:
                    pass

    def _copy_uldaq(self, start: int, count: int) -> np.ndarray:
        ptr = self._memhandle
        assert ptr is not None
        arr = np.ctypeslib.as_array(ptr, shape=(self._points_per_chan * len(self._cfg.channels),))  # type: ignore[arg-type]
        total = arr.size
        if start + count <= total:
            return arr[start : start + count].copy()
        first = arr[start:].copy()
        second = arr[: count - len(first)].copy()
        return np.concatenate((first, second))


__all__ = ["DaqStream", "DaqConfig", "DaqError"]

