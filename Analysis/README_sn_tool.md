
# sn_tool.py — Fatigue S–N Tool

This script processes large cyclic datasets to produce rainflow cycle counts, damage via Miner’s rule with mean-stress corrections, and fits S–N curves across tests. It can also estimate damage rates from PSD using the Dirlik method.

## Quick start

```bash
python sn_tool.py "/mnt/data/New_Code_Test_Cyclic - 3.3 .xlsx" \
  --time-col "Time (s)" --signal-col "Force (lbf)" \
  --to-stress-scale 1.0 --stress-unit "lbf" \
  --hysteresis-threshold 2.0 \
  --goodman-Su 60000 --msc-method Goodman \
  --export-cycles \
  --save-prefix out
```

For S–N fitting from multiple tests where you know the applied stress amplitudes and observed cycles to failure:

```bash
python sn_tool.py test1.csv test2.csv test3.csv \
  --sn-stress-levels "3000,2500,2000" \
  --sn-failure-cycles "1.2e5,6.0e5,2.4e6" \
  --goodman-Su 60000 --msc-method Walker --walker-gamma 0.5 \
  --save-prefix snfit
```

Enable PSD-based damage rate (Welch + Dirlik) if SciPy is available:

```bash
python sn_tool.py test1.csv --do-psd
```

## Notes

- Rainflow counting follows a 4-point stack algorithm (ASTM E1049-85 compatible).
- Mean stress corrections: Goodman, Gerber, Walker, Morrow. Set `--msc-method None` to disable.
- You can convert force/torque to stress via `--to-stress-scale` and `--to-stress-offset` (e.g., for area normalization).
- Histograms are saved as CSV with bin centers and counts.
- For extreme datasets, consider pre-converting to CSV and running with chunk processing in a future version.
