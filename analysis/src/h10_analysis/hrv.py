"""HRV metrics from cleaned NN intervals.

Definitions (all intervals in ms):

- mean_rr_ms, median_rr_ms, min_rr_ms, max_rr_ms over NN intervals.
- sdnn_ms: sample standard deviation (ddof=1) of NN intervals.
- rmssd_ms: sqrt(mean(d^2)) over valid successive differences d (see
  rr.successive_differences: both intervals retained, adjacent, same segment).
- pnn50_percent: 100 * count(|d| > 50 ms) / count(d).
- mad_ms: median absolute deviation from the median (unscaled).
- cv_percent: 100 * sdnn / mean_rr.
- mean_hr_bpm: mean of instantaneous heart rate 60000 / NN.
- Poincaré: SD1 = std(RR[n+1] - RR[n]) / sqrt(2), SD2 = std(RR[n+1] + RR[n]) / sqrt(2)
  (ddof=1), over the same valid pairs.

Frequency domain (only on a long, clean, gap-free segment):
retained beat times and NN values -> cubic spline at freq_resample_hz ->
linear detrend -> Welch PSD (Hann, 256 s segments or the whole segment if
shorter, 50 % overlap) -> band power by trapezoidal integration.
Bands: VLF 0.0033-0.04 Hz, LF 0.04-0.15 Hz, HF 0.15-0.40 Hz. VLF is only
reported for segments of at least 300 s. LF/HF is a ratio of two signal band
powers and is not interpreted physiologically here.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import interpolate, signal

from .models import AnalysisConfig
from .rr import successive_differences, successive_pairs

BANDS = {"vlf": (0.0033, 0.04), "lf": (0.04, 0.15), "hf": (0.15, 0.40)}


def time_domain(nn_ms: np.ndarray, diffs_ms: np.ndarray) -> dict:
    nn = np.asarray(nn_ms, dtype=float)
    d = np.asarray(diffs_ms, dtype=float)
    out: dict = {"nn_count": int(len(nn)), "successive_difference_count": int(len(d))}
    if len(nn) == 0:
        return out
    mean = float(nn.mean())
    sdnn = float(nn.std(ddof=1)) if len(nn) > 1 else None
    out.update({
        "valid_duration_s": float(nn.sum() / 1000),
        "mean_rr_ms": mean,
        "median_rr_ms": float(np.median(nn)),
        "min_rr_ms": float(nn.min()),
        "max_rr_ms": float(nn.max()),
        "sdnn_ms": sdnn,
        "mad_ms": float(np.median(np.abs(nn - np.median(nn)))),
        "cv_percent": 100 * sdnn / mean if sdnn is not None else None,
        "mean_hr_bpm": float(np.mean(60000 / nn)),
        "rmssd_ms": float(np.sqrt(np.mean(d ** 2))) if len(d) else None,
        "nn50_count": int(np.sum(np.abs(d) > 50)) if len(d) else None,
        "pnn50_percent": float(100 * np.mean(np.abs(d) > 50)) if len(d) else None,
    })
    return out


def poincare(x: np.ndarray, y: np.ndarray) -> dict:
    if len(x) < 3:
        return {"pairs": int(len(x)), "sd1_ms": None, "sd2_ms": None, "sd1_sd2_ratio": None}
    sd1 = float(np.std(y - x, ddof=1) / np.sqrt(2))
    sd2 = float(np.std(y + x, ddof=1) / np.sqrt(2))
    return {"pairs": int(len(x)), "sd1_ms": sd1, "sd2_ms": sd2, "sd1_sd2_ratio": sd1 / sd2 if sd2 else None}


def _sufficiency(n_total: int, nn: np.ndarray, cfg: AnalysisConfig) -> tuple[str, str | None]:
    artifact_pct = 100 * (n_total - len(nn)) / n_total if n_total else 100.0
    if artifact_pct > cfg.hrv_max_artifact_pct:
        return "too_many_artifacts", f"{artifact_pct:.1f} % of intervals excluded (limit {cfg.hrv_max_artifact_pct} %)"
    dur = nn.sum() / 1000
    if len(nn) < cfg.hrv_min_intervals or dur < cfg.hrv_min_duration_s:
        return "insufficient_data", (f"{len(nn)} valid intervals / {dur:.1f} s "
                                     f"(need {cfg.hrv_min_intervals} / {cfg.hrv_min_duration_s:.0f} s)")
    return "ok", None


def session_hrv(rr_flags: pd.DataFrame, cfg: AnalysisConfig) -> dict:
    """Whole-session time-domain and Poincaré metrics, if data is sufficient."""
    nn = rr_flags.loc[~rr_flags["excluded"], "rr_ms"].to_numpy(dtype=float)
    status, reason = _sufficiency(len(rr_flags), nn, cfg)
    out = {"status": status, "reason": reason}
    if status == "ok":
        out["time_domain"] = time_domain(nn, successive_differences(rr_flags))
        out["poincare"] = poincare(*successive_pairs(rr_flags))
    return out


def windowed_hrv(rr_flags: pd.DataFrame, cfg: AnalysisConfig, started_at: pd.Timestamp | None) -> pd.DataFrame:
    cols = ["window_start_timestamp", "window_start_s", "window_end_s", "status", "mean_hr_bpm", "rmssd_ms",
            "sdnn_ms", "valid_rr_count", "artifact_percentage", "coverage"]
    rows = []
    if len(rr_flags) < 2:
        return pd.DataFrame(rows, columns=cols)
    t = rr_flags["t"].to_numpy()
    W, step = cfg.hrv_window_s, cfg.hrv_step_s
    for ws in np.arange(t[0], t[-1] - W + 1e-9, step):
        m = (t >= ws) & (t < ws + W)
        win = rr_flags.loc[m]
        nn = win.loc[~win["excluded"], "rr_ms"].to_numpy(dtype=float)
        total = len(win)
        art = 100 * (total - len(nn)) / total if total else 100.0
        cov = nn.sum() / 1000 / W
        row = {"window_start_timestamp": "" if started_at is None else
               (started_at + pd.Timedelta(seconds=float(ws))).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
               "window_start_s": round(float(ws), 3), "window_end_s": round(float(ws + W), 3),
               "valid_rr_count": int(len(nn)), "artifact_percentage": round(art, 3), "coverage": round(cov, 4),
               "mean_hr_bpm": np.nan, "rmssd_ms": np.nan, "sdnn_ms": np.nan}
        if cov < cfg.hrv_window_min_coverage:
            row["status"] = "insufficient_coverage"
        elif art > cfg.hrv_window_max_artifact_pct:
            row["status"] = "too_many_artifacts"
        else:
            # Successive differences fully inside the window.
            win_ok = win["adjacent_prev"].to_numpy(dtype=bool).copy()
            win_ok[0] = False
            x = win["rr_ms"].to_numpy(dtype=float)
            d = (x[1:] - x[:-1])[win_ok[1:]]
            td = time_domain(nn, d)
            row.update(status="ok", mean_hr_bpm=td["mean_hr_bpm"], rmssd_ms=td["rmssd_ms"], sdnn_ms=td["sdnn_ms"])
        rows.append(row)
    return pd.DataFrame(rows, columns=cols)


def frequency_domain(rr_flags: pd.DataFrame, cfg: AnalysisConfig) -> dict:
    method = {
        "interpolation": "cubic spline (scipy CubicSpline, not-a-knot) of NN intervals at retained beat times",
        "resample_hz": cfg.freq_resample_hz,
        "detrend": "linear",
        "psd": "Welch, Hann window, 256 s segments (or whole segment if shorter), 50 % overlap, density scaling",
        "bands_hz": BANDS,
        "units": "ms^2",
    }
    if not cfg.freq_enabled:
        return {"status": "disabled", "method": method}
    best = None
    for seg, g in rr_flags.groupby("segment"):
        dur = g["t"].iloc[-1] - g["t"].iloc[0]
        art = 100 * g["excluded"].mean()
        if dur >= cfg.freq_min_segment_s and art <= cfg.freq_max_artifact_pct and (best is None or dur > best[1]):
            best = (seg, dur, art, g)
    if best is None:
        return {"status": "insufficient_data", "method": method,
                "reason": f"no gap-free segment of at least {cfg.freq_min_segment_s:.0f} s with at most "
                          f"{cfg.freq_max_artifact_pct} % excluded intervals"}
    seg, dur, art, g = best
    g = g.loc[~g["excluded"]]
    tb = g["t"].to_numpy()
    nn = g["rr_ms"].to_numpy(dtype=float)
    fs = cfg.freq_resample_hz
    grid = np.arange(tb[0], tb[-1], 1 / fs)
    x = signal.detrend(interpolate.CubicSpline(tb, nn)(grid), type="linear")
    nper = min(len(x), int(256 * fs))
    f, pxx = signal.welch(x, fs=fs, window="hann", nperseg=nper, noverlap=nper // 2, scaling="density")

    def band(lo: float, hi: float) -> float:
        m = (f >= lo) & (f < hi)
        return float(np.trapezoid(pxx[m], f[m])) if m.sum() > 1 else 0.0

    powers = {k: band(*v) for k, v in BANDS.items()}
    out = {
        "status": "ok", "method": method,
        "segment": {"start_s": float(tb[0]), "end_s": float(tb[-1]), "duration_s": float(dur),
                    "excluded_percentage": float(art)},
        "frequency_resolution_hz": float(f[1] - f[0]) if len(f) > 1 else None,
        "vlf_ms2": powers["vlf"] if dur >= 300 else None,
        "lf_ms2": powers["lf"],
        "hf_ms2": powers["hf"],
        "lf_hf_ratio": powers["lf"] / powers["hf"] if powers["hf"] > 0 else None,
        "lf_peak_hz": _peak(f, pxx, BANDS["lf"]),
        "hf_peak_hz": _peak(f, pxx, BANDS["hf"]),
    }
    return out


def _peak(f: np.ndarray, pxx: np.ndarray, band: tuple[float, float]) -> float | None:
    m = (f >= band[0]) & (f < band[1])
    return float(f[m][np.argmax(pxx[m])]) if m.any() else None
