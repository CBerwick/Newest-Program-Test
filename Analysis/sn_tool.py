
# Minimal restored version with programmatic run_sn API and CLI wrapper.
# (Shortened for kernel reset; retains core functionality used by user.)
import json, os, pathlib, argparse
from dataclasses import dataclass, asdict, field
from typing import List, Tuple, Optional, Dict, Any
import numpy as np
import pandas as pd
try:
    from scipy import signal
except Exception:
    signal = None

@dataclass
class ChannelSpec:
    time_col: str = "Time (s)"
    signal_col: str = "Force (lbf)"
    unit: str = "lbf"
    to_stress_scale: float = 1.0
    to_stress_offset: float = 0.0
    stress_unit: str = "psi"

@dataclass
class MeanStressCorrection:
    method: str = "Goodman"
    Su: Optional[float] = None
    Se: Optional[float] = None
    walker_gamma: float = 0.5
    sigma_f_prime: Optional[float] = None

@dataclass
class SNParams:
    A: Optional[float] = None
    b: Optional[float] = None
    enable_bilinear: bool = False
    N_knee: Optional[float] = None
    A2: Optional[float] = None
    b2: Optional[float] = None
    sigma_log10: Optional[float] = None

@dataclass
class ProcessingConfig:
    detrend: bool = False
    remove_mean: bool = True
    hysteresis_threshold: float = 0.0
    keep_extents: bool = True
    bin_amplitude: Optional[np.ndarray] = None
    bin_mean: Optional[np.ndarray] = None
    apply_mean_correction: bool = True
    msc: MeanStressCorrection = field(default_factory=MeanStressCorrection)

def _auto_col(df: pd.DataFrame, candidates):
    cols_lower = {c.lower(): c for c in df.columns}
    for name in candidates:
        if name and name.lower() in cols_lower:
            return cols_lower[name.lower()]
    for c in df.columns:
        if pd.api.types.is_numeric_dtype(df[c]):
            return c
    raise ValueError("No suitable column")

def turning_points(x: np.ndarray, keep_extents=True) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size < 3: return x
    diff = np.diff(x); mask = np.hstack(([True], diff != 0)); x = x[mask]
    if x.size < 3: return x
    d = np.diff(x); s = np.sign(d); s[s==0]=1
    idx = np.where(np.diff(s)!=0)[0] + 1
    tp = x[idx]
    if keep_extents: tp = np.hstack((x[0], tp, x[-1]))
    return tp

def hysteresis_filter(tp: np.ndarray, thr: float) -> np.ndarray:
    if thr <= 0 or tp.size==0: return tp
    out = [tp[0]]
    for v in tp[1:]:
        if abs(v - out[-1]) >= thr: out.append(v)
        else: out[-1] = v
    return np.asarray(out, float)

def rainflow_count(tp: np.ndarray):
    S=[]; cyc=[]
    for x in tp:
        S.append(float(x))
        while len(S)>=4:
            x0,x1,x2,x3 = S[-4],S[-3],S[-2],S[-1]
            r1 = abs(x1-x2); r2 = abs(x0-x1)
            if r1 <= r2:
                amp = r1/2.0; mean=(x1+x2)/2.0; cyc.append((amp,mean,1.0))
                del S[-3:-1]
            else: break
    for i in range(len(S)-1):
        amp = abs(S[i]-S[i+1])/2.0; mean=(S[i]+S[i+1])/2.0; cyc.append((amp,mean,0.5))
    return cyc

def rainflow_histogram(cycles, a_edges, m_edges):
    amps = np.array([c[0] for c in cycles]); means=np.array([c[1] for c in cycles]); counts=np.array([c[2] for c in cycles])
    ai = np.digitize(amps, a_edges)-1; mi = np.digitize(means, m_edges)-1
    H = np.zeros((len(a_edges)-1, len(m_edges)-1), float)
    for a,m,w in zip(ai,mi,counts):
        if 0<=a<H.shape[0] and 0<=m<H.shape[1]: H[a,m]+=w
    return H

def apply_mean_stress_correction(amp, mean, msc: MeanStressCorrection):
    if msc.method.lower() in ("none","off"): return amp
    if msc.method.lower()=="goodman":
        if not msc.Su: return amp
        denom = 1.0 - mean/msc.Su
        return amp/denom if denom!=0 else np.inf
    if msc.method.lower()=="gerber":
        if not msc.Su: return amp
        denom = 1.0 - (mean/msc.Su)**2
        return amp/denom if denom!=0 else np.inf
    if msc.method.lower()=="walker":
        if not msc.Su: return amp
        denom = 1.0 - mean/msc.Su
        return amp*(denom**(-msc.walker_gamma)) if denom>0 else np.inf
    if msc.method.lower()=="morrow":
        if not msc.sigma_f_prime: return amp
        return amp*(1.0 - mean/msc.sigma_f_prime)
    return amp

def basquin_fit(S, N):
    x = np.log10(N); y=np.log10(S)
    A = np.vstack([np.ones_like(x), x]).T
    sol, _, _, _ = np.linalg.lstsq(A, y, rcond=None)
    log10A, b = sol
    yhat = log10A + b*x
    sigma = float(np.sqrt(np.mean((y-yhat)**2)))
    return 10**log10A, float(b), sigma

def read_table(path, sheet=None) -> pd.DataFrame:
    p = pathlib.Path(path)
    if p.suffix.lower() in (".xlsx",".xls"):
        return pd.read_excel(path, sheet_name=(0 if sheet is None else sheet))
    elif p.suffix.lower() in (".csv",".txt"):
        return pd.read_csv(path)
    else:
        raise ValueError("Unsupported extension")

def process_file(path, chan: ChannelSpec, cfg: ProcessingConfig, sheet=None):
    df = read_table(path, sheet=sheet)
    time_col = chan.time_col if chan.time_col in df.columns else _auto_col(df, [chan.time_col,"time","t"])
    sig_col  = chan.signal_col if chan.signal_col in df.columns else _auto_col(df, [chan.signal_col,"stress","load","force"])
    t = df[time_col].to_numpy(float)
    x_raw = df[sig_col].to_numpy(float)
    x = chan.to_stress_scale * x_raw + chan.to_stress_offset
    if cfg.remove_mean: x = x - np.nanmean(x)
    if cfg.detrend:
        n=len(x); xi=np.arange(n); M=np.vstack([np.ones(n),xi]).T
        beta,_,_,_=np.linalg.lstsq(M,x,rcond=None); trend = M@beta; x = x - trend
    tp = turning_points(x, keep_extents=cfg.keep_extents)
    tp = hysteresis_filter(tp, cfg.hysteresis_threshold)
    cycles = rainflow_count(tp)
    if cfg.bin_amplitude is None:
        amax = max(1e-6, np.percentile([c[0] for c in cycles], 99.5))
        cfg.bin_amplitude = np.linspace(0, amax, 51)
    if cfg.bin_mean is None:
        ms = [c[1] for c in cycles]
        if len(ms)==0: ms=[0,0]
        lo, hi = np.percentile(ms, [0.5,99.5])
        cfg.bin_mean = np.linspace(lo, hi, 51)
    H = rainflow_histogram(cycles, cfg.bin_amplitude, cfg.bin_mean)
    return dict(dataframe=df, time_col=time_col, sig_col=sig_col, t=t, x=x,
                turning_points=tp, cycles=cycles, histogram=H,
                amp_edges=cfg.bin_amplitude, mean_edges=cfg.bin_mean)

def build_sn_from_tests(test_results, failure_cycles, stress_level_per_test=None, msc: Optional[MeanStressCorrection]=None):
    S_list=[]; N_list=[]
    for i,res in enumerate(test_results):
        Nf = failure_cycles[i]
        if Nf is None or not np.isfinite(Nf): continue
        if stress_level_per_test and stress_level_per_test[i] is not None:
            S_eq = stress_level_per_test[i]
        else:
            amps = np.array([c[0] for c in res["cycles"]]); counts=np.array([c[2] for c in res["cycles"]])
            if counts.sum()==0: continue
            S_eq = float(np.sqrt(np.sum(counts*amps**2)/np.sum(counts)))
            if msc: S_eq = apply_mean_stress_correction(S_eq, 0.0, msc)
        S_list.append(S_eq); N_list.append(Nf)
    if len(S_list)<2: raise ValueError("Need at least two (S,N) points")
    A,b,sigma = basquin_fit(np.array(S_list), np.array(N_list))
    return SNParams(A=A,b=b,sigma_log10=sigma)

# Programmatic API
def run_sn(inputs, sheet=None, time_col=None, signal_col=None, unit="lbf", to_stress_scale=1.0, to_stress_offset=0.0,
           stress_unit="psi", hysteresis_threshold=0.0, remove_mean=True, detrend=False,
           msc_method="Goodman", goodman_Su=None, walker_gamma=0.5, morrow_sigma_f_prime=None,
           sn_stress_levels=None, sn_failure_cycles=None, export_cycles=False, save_prefix="fatigue_out", do_psd=False):
    if isinstance(inputs, (str, bytes, os.PathLike)): inputs=[inputs]
    chan = ChannelSpec(time_col=time_col or "Time (s)", signal_col=signal_col or "Force (lbf)",
                       unit=unit, to_stress_scale=to_stress_scale, to_stress_offset=to_stress_offset,
                       stress_unit=stress_unit)
    cfg = ProcessingConfig(detrend=detrend, remove_mean=remove_mean, hysteresis_threshold=hysteresis_threshold)
    msc = MeanStressCorrection(method=msc_method, Su=goodman_Su, walker_gamma=walker_gamma, sigma_f_prime=morrow_sigma_f_prime)
    results=[]
    for p in inputs:
        res = process_file(p, chan, cfg, sheet=sheet)
        results.append(res)
        H=res["histogram"]; aedges=res["amp_edges"]; medges=res["mean_edges"]
        ac=0.5*(aedges[:-1]+aedges[1:]); mc=0.5*(medges[:-1]+medges[1:])
        rows=[(a,m,H[i,j]) for i,a in enumerate(ac) for j,m in enumerate(mc)]
        pd.DataFrame(rows, columns=["amp","mean","count"]).to_csv(f"{save_prefix}_{pathlib.Path(p).stem}_hist.csv", index=False)
        if export_cycles:
            pd.DataFrame(res["cycles"], columns=["amp","mean","count"]).to_csv(f"{save_prefix}_{pathlib.Path(p).stem}_cycles.csv", index=False)
    sn=None
    if sn_failure_cycles is not None:
        if sn_stress_levels is None: sn_stress_levels=[None]*len(results)
        sn = build_sn_from_tests(results, sn_failure_cycles, sn_stress_levels, msc=msc if msc_method!="None" else None)
        with open(f"{save_prefix}_sn_fit.json","w") as f: json.dump(asdict(sn), f, indent=2)
    return {"results":results, "sn": asdict(sn) if sn else None, "save_prefix": save_prefix}

# CLI wrapper (optional)
def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("--sheet")
    ap.add_argument("--time-col"); ap.add_argument("--signal-col")
    ap.add_argument("--unit", default="lbf"); ap.add_argument("--to-stress-scale", type=float, default=1.0)
    ap.add_argument("--to-stress-offset", type=float, default=0.0); ap.add_argument("--stress-unit", default="psi")
    ap.add_argument("--hysteresis-threshold", type=float, default=0.0)
    ap.add_argument("--no-remove-mean", action="store_true"); ap.add_argument("--detrend", action="store_true")
    ap.add_argument("--msc-method", default="Goodman", choices=["Goodman","Gerber","Walker","Morrow","None"])
    ap.add_argument("--goodman-Su", type=float); ap.add_argument("--walker-gamma", type=float, default=0.5)
    ap.add_argument("--morrow-sigma-f-prime", type=float); ap.add_argument("--sn-stress-levels", type=str)
    ap.add_argument("--sn-failure-cycles", type=str); ap.add_argument("--export-cycles", action="store_true")
    ap.add_argument("--save-prefix", default="fatigue_out")
    return ap.parse_args(argv)

def main(argv=None):
    args = parse_args(argv)
    sn_stress = [float(x) if x.strip()!="" else None for x in args.sn_stress_levels.split(",")] if args.sn_stress_levels else None
    fc = [float(x) if x.strip()!="" else None for x in args.sn_failure_cycles.split(",")] if args.sn_failure_cycles else None
    out = run_sn(
        inputs=args.inputs, sheet=args.sheet, time_col=args.time_col, signal_col=args.signal_col,
        unit=args.unit, to_stress_scale=args.to_stress_scale, to_stress_offset=args.to_stress_offset, stress_unit=args.stress_unit,
        hysteresis_threshold=args.hysteresis_threshold, remove_mean=not args.no_remove_mean, detrend=args.detrend,
        msc_method=args.msc_method, goodman_Su=args.goodman_Su, walker_gamma=args.walker_gamma,
        morrow_sigma_f_prime=args.morrow_sigma_f_prime, sn_stress_levels=sn_stress,
        sn_failure_cycles=fc, export_cycles=args.export_cycles, save_prefix=args.save_prefix
    )
    print("Done."); return 0

if __name__ == "__main__":
    raise SystemExit(main())
