# -*- coding: utf-8 -*-
"""
Created on Fri Aug 22 14:41:33 2025

@author: cberwick
"""

import pandas as pd
import numpy as np

# --- edit this to your file path ---
xlsx_path = r"C:\Cloud Folders\Cloud - Oilfield Formulation\~Raw Test Data~\Shear Coupons\Lab Testing\008\8.8-4.1__Cube-3__Cyclic-1__8-22-25.xlsx"

# Load data (assumes the first row is headers)
df = pd.read_excel(xlsx_path)

# First column = time [s], third column = force [lbf]
t = df.iloc[:, 0].to_numpy(dtype=float)
f = df.iloc[:, 2].to_numpy(dtype=float)

# Keep only 70s to 280s
mask = (t >= 70.0) & (t <= 280.0)
t_win = t[mask]
f_win = f[mask]

# Basic guard
if t_win.size < 2:
    raise ValueError("Not enough samples between 70s and 280s.")

# De-mean to make zero-crossings meaningful
f0 = f_win - np.nanmean(f_win)

# Count positive-going zero crossings
crossings = np.sum((f0[:-1] <= 0) & (f0[1:] > 0))

# Average frequency in Hz = cycles / duration
duration = t_win[-1] - t_win[0]
avg_freq_hz = crossings / duration

print(f"Average frequency (70–280 s): {avg_freq_hz:.6f} Hz  (~{60*avg_freq_hz:.3f} cycles/min)")
