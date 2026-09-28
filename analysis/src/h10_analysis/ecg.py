"""ECG integrity, filtering, R-peak detection and RR cross-check.

The raw ECG (ecg_uv) is never modified. Filtering produces a separate array
used for R-peak detection and clearly labelled plots.

Filter (documented in metrics.json): Butterworth band-pass
ecg_highpass_hz-ecg_lowpass_hz (default 0.5-40 Hz), order ecg_filter_order,
applied forward and backward (scipy sosfiltfilt: zero phase, effective order
doubled). It is applied separately to each continuous segment (split at ECG
gaps) so that no filter output spans missing data. No notch filter: mains
frequencies (50/60 Hz) are above the 40 Hz low-pass corner. The sample rate is
the measured median sample spacing, which follows the sensor clock.

R-peak detection (per continuous segment): 5-20 Hz band-pass of the raw
signal, derivative, squaring, 150 ms moving average (Pan-Tompkins style
energy envelope); peaks at least 250 ms apart above 30 % of the local
(5 s rolling) 98th percentile of the envelope; each peak is then located at
the extremum of the 0.5-40 Hz filtered signal (dominant polarity) within
+-80 ms and refined to sub-sample precision by parabolic interpolation.
This is a data-integrity tool, not a validated clinical detector.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy import signal

from .gaps import ecg_period_s
from .models import AnalysisConfig, Session


def segments(ecg: pd.DataFrame, gaps: pd.DataFrame) -> np.ndarray:
    """Segment id per ECG row; a new segment starts after each internal ECG gap."""
    t = ecg["t"].to_numpy()
    starts = gaps.loc[(gaps["stream"] == "ecg") & (gaps["kind"] == "internal"), "end_elapsed_s"].to_numpy()
    seg = np.zeros(len(t), dtype=int)
    for st in np.sort(starts):
        seg[np.searchsorted(t, st - 1e-9):] += 1
    return seg


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[start, end) index ranges where mask is True."""
    if not mask.any():
        return []
    d = np.diff(np.r_[0, mask.astype(np.int8), 0])
    return list(zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1), strict=True))


def integrity(s: Session, gaps: pd.DataFrame, cfg: AnalysisConfig) -> dict:
    ecg = s.ecg
    rate_meta = s.ecg_sample_rate_hz
    out: dict = {"samples": int(len(ecg)), "sample_rate_metadata_hz": rate_meta}
    if len(ecg) < 2:
        return out
    period = ecg_period_s(s)
    dt = np.diff(ecg["elapsed_ns"].to_numpy()) / 1e6
    g = gaps[(gaps["stream"] == "ecg") & (gaps["kind"] == "internal")]
    missing = int(g["missing_samples"].sum())
    out.update({
        "expected_period_ms": 1000 / rate_meta if rate_meta else None,
        "measured_period_ms": period * 1000,
        "measured_sample_rate_hz": 1 / period,
        "rate_deviation_percent": 100 * (1 / period - rate_meta) / rate_meta if rate_meta else None,
        "spacing_ms": {"min": float(dt.min()), "p1": float(np.percentile(dt, 1)), "median": float(np.median(dt)),
                       "p99": float(np.percentile(dt, 99)), "max": float(dt.max())},
        "non_increasing_steps": int(np.sum(dt <= 0)),
        "compressed_steps": int(np.sum((dt > 0) & (dt < 0.5 * period * 1000))),
        "gaps": int(len(g)),
        "missing_samples": missing,
        "missing_percentage": 100 * missing / (len(ecg) + missing),
        "duplicate_sample_index": int(np.sum(np.diff(ecg["sample_index"].to_numpy()) == 0)),
        "continuous": len(g) == 0 and bool(np.all(np.diff(ecg["sample_index"].to_numpy()) == 1)),
    })

    v = ecg["ecg_uv"].to_numpy(dtype=float)
    t = ecg["t"].to_numpy()
    flat_n = max(int(cfg.ecg_flat_s / period), 2)
    same = np.r_[False, np.diff(v) == 0]
    flat = []
    for a, b in _runs(same):
        if b - (a - 1) >= flat_n:
            flat.append({"start_s": float(t[a - 1]), "end_s": float(t[b - 1]), "samples": int(b - a + 1),
                         "value_uv": float(v[a])})
    extreme_mask = np.abs(v) > cfg.ecg_abs_limit_uv
    extreme = [{"start_s": float(t[a]), "end_s": float(t[b - 1]), "samples": int(b - a),
                "max_abs_uv": float(np.abs(v[a:b]).max())} for a, b in _runs(extreme_mask)]
    out.update({
        "amplitude_uv": {"min": float(v.min()), "p1": float(np.percentile(v, 1)), "median": float(np.median(v)),
                         "p99": float(np.percentile(v, 99)), "max": float(v.max())},
        "flatline_regions": flat,
        "extreme_value_samples": int(extreme_mask.sum()),
        "extreme_regions": _merge(extreme, 1.0),
    })
    return out


def _merge(regions: list[dict], within_s: float) -> list[dict]:
    merged: list[dict] = []
    for r in regions:
        if merged and r["start_s"] - merged[-1]["end_s"] <= within_s:
            merged[-1]["end_s"] = r["end_s"]
            merged[-1]["samples"] += r["samples"]
            merged[-1]["max_abs_uv"] = max(merged[-1]["max_abs_uv"], r["max_abs_uv"])
        else:
            merged.append(dict(r))
    return merged


def filtered(ecg: pd.DataFrame, seg: np.ndarray, fs: float, cfg: AnalysisConfig) -> np.ndarray:
    """Zero-phase band-pass per segment. NaN where a segment is too short."""
    return _bandpass(ecg["ecg_uv"].to_numpy(dtype=float), seg, fs, cfg.ecg_highpass_hz, cfg.ecg_lowpass_hz,
                     cfg.ecg_filter_order)


def _bandpass(x: np.ndarray, seg: np.ndarray, fs: float, lo: float, hi: float, order: int) -> np.ndarray:
    sos = signal.butter(order, [lo, min(hi, 0.45 * fs)], btype="bandpass", fs=fs, output="sos")
    y = np.full(len(x), np.nan)
    pad = 3 * (2 * len(sos) + 1)
    for k in np.unique(seg):
        idx = np.flatnonzero(seg == k)
        if len(idx) > pad * 2:
            y[idx] = signal.sosfiltfilt(sos, x[idx])
    return y


def filter_description(fs: float, cfg: AnalysisConfig) -> dict:
    return {
        "enabled": True,
        "type": "Butterworth band-pass, zero-phase (sosfiltfilt), applied per continuous segment",
        "highpass_hz": cfg.ecg_highpass_hz,
        "lowpass_hz": min(cfg.ecg_lowpass_hz, 0.45 * fs),
        "order": cfg.ecg_filter_order,
        "notch": None,
        "sample_rate_hz": fs,
        "purpose": "R-peak detection and labelled 'filtered' plots only; raw ECG unchanged",
    }


def detect_rpeaks(ecg: pd.DataFrame, filt: np.ndarray, seg: np.ndarray, fs: float) -> pd.DataFrame:
    cols = ["sample_index", "source_row", "elapsed_ns", "t", "ecg_uv", "filtered_uv", "segment"]
    raw = ecg["ecg_uv"].to_numpy(dtype=float)
    t = ecg["t"].to_numpy()
    energy = _bandpass(raw, seg, fs, 5.0, 20.0, 2)
    peaks_all: list[int] = []
    t_sub: list[float] = []
    for k in np.unique(seg):
        idx = np.flatnonzero((seg == k) & ~np.isnan(energy) & ~np.isnan(filt))
        if len(idx) < 3 * fs:
            continue
        f = filt[idx]
        # Dominant polarity of QRS complexes.
        sign = 1.0 if np.percentile(f, 99.5) >= -np.percentile(f, 0.5) else -1.0
        e = np.gradient(energy[idx]) ** 2
        w = max(int(0.15 * fs), 1)
        env = np.convolve(e, np.ones(w) / w, mode="same")
        thr = 0.3 * pd.Series(env).rolling(int(5 * fs), center=True, min_periods=1).quantile(0.98).to_numpy()
        cand, _ = signal.find_peaks(env, height=thr, distance=max(int(0.25 * fs), 1))
        half = int(0.08 * fs)
        last = -10**9
        for c in cand:
            a, b = max(c - half, 0), min(c + half + 1, len(f))
            p = a + int(np.argmax(sign * f[a:b]))
            if p - last < int(0.25 * fs):
                continue
            last = p
            # Parabolic interpolation of the peak time.
            frac = 0.0
            if 0 < p < len(f) - 1:
                y0, y1, y2 = sign * f[p - 1], sign * f[p], sign * f[p + 1]
                den = y0 - 2 * y1 + y2
                if den != 0:
                    frac = float(np.clip(0.5 * (y0 - y2) / den, -0.5, 0.5))
            gi = idx[p]
            nb = idx[min(p + 1, len(idx) - 1)] if frac >= 0 else idx[max(p - 1, 0)]
            peaks_all.append(gi)
            t_sub.append(t[gi] + abs(frac) * (t[nb] - t[gi]))
    p = np.array(peaks_all, dtype=int)
    out = pd.DataFrame({
        "sample_index": ecg["sample_index"].to_numpy()[p] if len(p) else [],
        "source_row": ecg["row"].to_numpy()[p] if len(p) else [],
        "elapsed_ns": np.round(np.array(t_sub) * 1e9).astype("int64") if len(p) else [],
        "t": np.array(t_sub),
        "ecg_uv": raw[p] if len(p) else [],
        "filtered_uv": filt[p] if len(p) else [],
        "segment": seg[p] if len(p) else [],
    }, columns=cols)
    return out


def ecg_rr(rpeaks: pd.DataFrame) -> pd.DataFrame:
    """RR intervals between consecutive R-peaks in the same segment; t = ending beat."""
    t = rpeaks["t"].to_numpy()
    same = np.r_[False, np.diff(rpeaks["segment"].to_numpy()) == 0]
    rr = np.r_[np.nan, np.diff(t) * 1000]
    df = pd.DataFrame({"t": t, "rr_ms": rr})
    return df.loc[same].reset_index(drop=True)


def compare_rr(rr_flags: pd.DataFrame, rpeaks: pd.DataFrame, ecg_spans: list[tuple[float, float]],
               cfg: AnalysisConfig) -> tuple[pd.DataFrame, dict]:
    """Match Polar RR beats to ECG R-peaks and compare interval lengths.

    Polar RR timestamps are estimates (see h10 docs/data-format.md) and can be
    offset from the ECG by more than one beat (the sensor reports RR with a
    delay). Beats are therefore aligned in two steps: a global time offset
    chosen from a grid (+-rr_align_search_s, 10 ms steps) to minimise the
    median |Polar RR - ECG RR| of matched beats, then beat-by-beat tracking of
    the offset from a well-matched anchor beat, updated only on beats whose
    intervals agree, so it follows slow drift without jumping to a
    neighbouring beat.
    """
    cols = ["t", "polar_rr_ms", "ecg_rr_ms", "diff_ms", "offset_ms", "status", "polar_reason"]
    pt = rr_flags["t"].to_numpy()
    prr = rr_flags["rr_ms"].to_numpy(dtype=float)
    et = rpeaks["t"].to_numpy()
    if len(pt) == 0 or len(et) < 2:
        return pd.DataFrame(columns=cols), {"status": "not_enough_beats"}
    er = np.r_[np.nan, np.diff(et) * 1000]
    er[np.r_[True, np.diff(rpeaks["segment"].to_numpy()) != 0]] = np.nan  # no RR across ECG gaps
    tol = cfg.rpeak_match_tolerance_ms / 1000

    def nearest(target: np.ndarray) -> np.ndarray:
        pos = np.clip(np.searchsorted(et, target), 1, len(et) - 1)
        return np.where(np.abs(et[pos - 1] - target) < np.abs(et[pos] - target), pos - 1, pos)

    # 1. Global alignment by RR values: in steady rhythm nearest-in-time
    #    matching is ambiguous by whole beats, so pick the offset whose
    #    matched intervals agree best.
    best = (np.inf, 0.0)
    for delta in np.arange(-cfg.rr_align_search_s, cfg.rr_align_search_s + 1e-9, 0.01):
        j = nearest(pt + delta)
        ok = (np.abs(et[j] - (pt + delta)) <= tol) & ~np.isnan(er[j])
        if ok.sum() >= 0.5 * len(pt):
            score = float(np.median(np.abs(prr[ok] - er[j][ok])))
            if score < best[0] - 1e-9:
                best = (score, float(delta))
    # 2. Beat-by-beat tracking from a well-matched anchor, forwards and
    #    backwards, follows slow drift of the Polar timestamp estimate (the
    #    recorder slews it by at most 20 ms per notification).
    j = nearest(pt + best[1])
    good = np.flatnonzero((np.abs(et[j] - pt - best[1]) <= tol) & (np.abs(prr - er[j]) <= cfg.rr_mismatch_ms))
    offset = np.full(len(pt), best[1])
    if len(good):
        anchor = int(good[len(good) // 2])
        for order in (range(anchor, len(pt)), range(anchor, -1, -1)):
            o = best[1]
            for i in order:
                k = nearest(np.array([pt[i] + o]))[0]
                if abs(et[k] - pt[i] - o) <= tol and abs(prr[i] - er[k]) <= cfg.rr_mismatch_ms:
                    o = et[k] - pt[i]
                offset[i] = o

    target = pt + offset
    near = nearest(target)
    matched = np.abs(et[near] - target) <= tol
    ecg_val = er[near]
    diff = prr - ecg_val

    # Beats whose (corrected) time is not covered by continuous ECG cannot be
    # compared; that is a coverage limit, not a disagreement.
    covered = np.zeros(len(pt), dtype=bool)
    for a, b in ecg_spans:
        covered |= (target >= a) & (target <= b)
    status = np.where(~covered, "outside_ecg",
                      np.where(~matched, "unmatched",
                               np.where(np.isnan(ecg_val), "no_ecg_interval",
                                        np.where(np.abs(diff) > cfg.rr_mismatch_ms, "mismatch", "match"))))
    matched &= covered
    comp = pd.DataFrame({"t": pt, "polar_rr_ms": prr, "ecg_rr_ms": np.where(matched, ecg_val, np.nan),
                         "diff_ms": np.where(matched, diff, np.nan), "offset_ms": offset * 1000,
                         "status": status, "polar_reason": rr_flags["reason"].to_numpy()}, columns=cols)
    ok = comp["status"].isin(["match", "mismatch"])
    dd = comp.loc[ok, "diff_ms"].to_numpy()
    used = set(near[matched].tolist())
    summary = {
        "status": "ok",
        "polar_beats": int(len(pt)),
        "ecg_beats": int(len(et)),
        "compared": int(ok.sum()),
        "matched_within_tolerance": int((comp["status"] == "match").sum()),
        "mismatched": int((comp["status"] == "mismatch").sum()),
        "unmatched_polar_beats": int((comp["status"] == "unmatched").sum()),
        "polar_beats_outside_ecg": int((comp["status"] == "outside_ecg").sum()),
        "ecg_beats_without_polar_beat": int(len(et) - len(used)),
        "mean_difference_ms": float(dd.mean()) if len(dd) else None,
        "median_difference_ms": float(np.median(dd)) if len(dd) else None,
        "mean_absolute_error_ms": float(np.abs(dd).mean()) if len(dd) else None,
        "max_absolute_error_ms": float(np.abs(dd).max()) if len(dd) else None,
        "median_timestamp_offset_ms": float(np.median(offset) * 1000),
        "match_tolerance_ms": cfg.rpeak_match_tolerance_ms,
        "mismatch_threshold_ms": cfg.rr_mismatch_ms,
        "divergence_periods": divergence_periods(comp),
    }
    return comp, summary


def divergence_periods(comp: pd.DataFrame, join_s: float = 10.0) -> list[dict]:
    """Group mismatched/unmatched beats closer than join_s into periods, on the
    ECG time axis (Polar beat time + alignment offset)."""
    bad = comp[comp["status"].isin(["mismatch", "unmatched"])]
    t_ecg = (bad["t"] + bad["offset_ms"] / 1000).to_numpy()
    periods: list[dict] = []
    for t in t_ecg:
        if periods and t - periods[-1]["end_s"] <= join_s:
            periods[-1]["end_s"] = float(t)
            periods[-1]["beats"] += 1
        else:
            periods.append({"start_s": float(t), "end_s": float(t), "beats": 1})
    return periods


def spans(ecg: pd.DataFrame, seg: np.ndarray) -> list[tuple[float, float]]:
    """(start_s, end_s) of each continuous ECG segment."""
    t = ecg["t"].to_numpy()
    return [(float(t[seg == k][0]), float(t[seg == k][-1])) for k in np.unique(seg)]
