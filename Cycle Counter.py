# -*- coding: utf-8 -*-
"""
Created on Fri Aug 22 14:47:54 2025

@author: cberwick
"""

import pandas as pd
import numpy as np

def count_cycles_excel(xlsx_path, sheet_name=0, thr_frac=0, tmin=None, tmax=None):
    """
    Count full cycles in a cyclic force-vs-time signal using a simple hysteresis method.
    By default, analyzes the entire dataset. Optionally restrict with tmin/tmax [s].

    - One cycle is counted each time the signal crosses from <= -thr to >= +thr.
    - 'thr' = thr_frac * robust amplitude range (5th–95th percentile) after de-meaning.
    """
    df = pd.read_excel(xlsx_path, sheet_name=sheet_name)

    # First column = time [s], third column = force [lbf]
    t = df.iloc[:, 0].to_numpy(dtype=float)
    f = df.iloc[:, 2].to_numpy(dtype=float)

    # Optional time window; default is full length
    mask = np.isfinite(t) & np.isfinite(f)
    if tmin is not None:
        mask &= (t >= float(tmin))
    if tmax is not None:
        mask &= (t <= float(tmax))

    t_win = t[mask]
    f_win = f[mask]
    if t_win.size < 2:
        raise ValueError("Not enough samples in the selected (or full) time range.")

    # De-mean and set hysteresis
    f0 = f_win - np.nanmedian(f_win)
    p5, p95 = np.nanpercentile(f0, [5, 95])
    amp_range = max(1e-12, (p95 - p5))
    thr = thr_frac * amp_range

    # Three-state track: -1 (low), 0 (mid), +1 (high)
    s = np.zeros_like(f0, dtype=int)
    s[f0 >= +thr] = +1
    s[f0 <= -thr] = -1

    # Count transitions low -> high as one full cycle
    cycles = int(np.sum((s[:-1] == -1) & (s[1:] == +1)))

    print(f"Cycles (full dataset{'' if (tmin is None and tmax is None) else f' {t_win[0]:.3f}s–{t_win[-1]:.3f}s'}): {cycles}")
    return cycles

# --- usage ---
cycles = count_cycles_excel(r"C:\Cloud Folders\Cloud - Oilfield Formulation\~Raw Test Data~\Shear Coupons\Lab Testing\008\8.8-4.1__Cube-3__Cyclic-1__8-22-25.xlsx")  # full dataset
cycles_70_280 = count_cycles_excel(r"C:\Cloud Folders\Cloud - Oilfield Formulation\~Raw Test Data~\Shear Coupons\Lab Testing\008\8.8-4.1__Cube-3__Cyclic-1__8-22-25.xlsx", tmin=70, tmax=280)
