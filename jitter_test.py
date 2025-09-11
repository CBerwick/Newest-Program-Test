"""Simple CLI to verify deterministic hardware-paced sampling.

Streams analog input for a specified duration and computes basic jitter
statistics.  Results are written to CSV with monotonic timestamps.
"""
import argparse
import csv
import time
import numpy as np

from daq_stream import DaqConfig, DaqStream


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fs", type=float, default=20.0, help="Sample rate per channel")
    parser.add_argument("--duration", type=float, default=60.0, help="Streaming duration in seconds")
    parser.add_argument("--channels", type=int, nargs="+", default=[0], help="Analog input channels")
    parser.add_argument("--output", default="jitter_test.csv", help="CSV output path")
    args = parser.parse_args()

    stream = DaqStream()
    cfg = DaqConfig(channels=args.channels, fs=args.fs)
    stream.start(cfg)
    end_time = stream.t0 + args.duration

    timestamps: list[float] = []
    samples: list[np.ndarray] = []
    while time.monotonic() < end_time:
        block = stream.read()
        if block is None:
            time.sleep(0.01)
            continue
        ts, data = block
        timestamps.extend(ts)
        samples.append(data)
    stream.stop()

    if samples:
        data = np.vstack(samples)
        ts = np.array(timestamps)
        dt = np.diff(ts)
        if dt.size:
            print(f"Δt mean={dt.mean():.6f}s std={dt.std():.6f}s max={dt.max():.6f}s")
            hist, edges = np.histogram(dt, bins=10)
            for h, e1, e2 in zip(hist, edges[:-1], edges[1:]):
                print(f"{e1:.6f}-{e2:.6f}s: {h}")
        with open(args.output, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp"] + [f"ch{c}" for c in args.channels])
            for t, row in zip(ts, data):
                writer.writerow([t] + row.tolist())
        print(f"Wrote {data.shape[0]} samples to {args.output}")
    else:
        print("No samples collected")


if __name__ == "__main__":
    main()
