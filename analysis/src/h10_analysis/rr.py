"""RR interval artifact detection and cleaning.

Raw RR values are never modified. Each interval gets zero or more reasons;
some reasons exclude the interval from the cleaned (NN) series, others only
flag it for review. No interpolation is performed.

Excluding reasons (conservative, applied in this order):

- ``duplicate_timestamp``: same elapsed_ns as the previous interval.
- ``out_of_range``: rr_ms < rr_min_ms or > rr_max_ms.
- ``possible_missed_beat``: deviates from the local median and is ~2x it
  (a beat between two detections was likely not detected).
- ``possible_extra_beat``: two consecutive short intervals that together are
  ~1 local median (a spurious detection split one interval).
- ``short_long_pair``: an interval deviating by more than the threshold,
  followed by one deviating in the opposite direction by more than half the
  threshold, together ~2 local medians (a mistimed detection; the same pattern
  is produced by premature beats, which are also excluded from NN series by
  convention).
- ``local_deviation``: any other deviation from the local median by more than
  rr_local_threshold (isolated extreme value / abrupt jump).

Flag-only reason:

- ``near_gap``: within rr_near_gap_s of a recording gap or connection event.

The local median is a centred rolling median over rr_local_window intervals,
ignoring intervals already excluded as duplicate or out of range.

Successive differences (RMSSD, pNN50, Poincaré) are only formed between two
retained intervals that were adjacent in the original series and belong to the
same continuous segment; a new segment starts after every RR gap.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import AnalysisConfig, Session

EXCLUDING = ("duplicate_timestamp", "out_of_range", "possible_missed_beat", "possible_extra_beat",
             "short_long_pair", "local_deviation")
FLAG_ONLY = ("near_gap",)
BOUNDARY_EVENTS = {"connection_lost", "device_reconnected", "rr_chain_rebased", "packets_dropped", "data_stalled"}


def analyze_rr(s: Session, gaps: pd.DataFrame, cfg: AnalysisConfig) -> pd.DataFrame:
    """Return one row per RR interval with flags, exclusion and segment info."""
    rr = s.rr
    n = len(rr)
    out = pd.DataFrame({
        "source_row": rr["row"].to_numpy(),
        "ts": rr["ts"].to_numpy(),
        "elapsed_ns": rr["elapsed_ns"].to_numpy(),
        "t": rr["t"].to_numpy(),
        "rr_ms": rr["rr_ms"].to_numpy(),
    })
    reasons: list[list[str]] = [[] for _ in range(n)]
    if n == 0:
        for c in ("local_median_ms", "segment", "excluded", "adjacent_prev"):
            out[c] = pd.Series(dtype="float64")
        out["reason"] = pd.Series(dtype="string")
        return out

    x = out["rr_ms"].to_numpy(dtype=float)
    e = out["elapsed_ns"].to_numpy()

    hard = np.zeros(n, dtype=bool)
    dup = np.r_[False, np.diff(e) == 0]
    rng = np.isnan(x) | (x < cfg.rr_min_ms) | (x > cfg.rr_max_ms)
    for i in np.flatnonzero(dup):
        reasons[i].append("duplicate_timestamp")
    for i in np.flatnonzero(rng):
        reasons[i].append("out_of_range")
    hard |= dup | rng

    med = pd.Series(np.where(hard, np.nan, x)).rolling(
        cfg.rr_local_window, center=True, min_periods=3).median().to_numpy()
    out["local_median_ms"] = med
    with np.errstate(invalid="ignore", divide="ignore"):
        ratio = x / med
    dev = ~hard & ~np.isnan(ratio) & (np.abs(ratio - 1) > cfg.rr_local_threshold)

    tol = cfg.rr_pattern_tolerance
    labelled = np.zeros(n, dtype=bool)
    for i in np.flatnonzero(dev):
        if labelled[i]:
            continue
        if abs(ratio[i] - 2) <= 2 * tol:
            reasons[i].append("possible_missed_beat")
            labelled[i] = True
            continue
        j = i + 1  # pair patterns with the next interval
        if j < n and not hard[j] and not np.isnan(ratio[j]):
            pair = (x[i] + x[j]) / med[i]
            if ratio[i] < 1 and ratio[j] < 1 and abs(pair - 1) <= tol:
                for k in (i, j):
                    reasons[k].append("possible_extra_beat")
                labelled[[i, j]] = True
                continue
            # The partner only needs half the deviation threshold: after a
            # short interval the following one is typically long but not
            # always by the full threshold (incomplete compensation).
            partner = abs(ratio[j] - 1) > cfg.rr_local_threshold / 2
            if (ratio[i] - 1) * (ratio[j] - 1) < 0 and partner and abs(pair - 2) <= 2 * tol:
                for k in (i, j):
                    reasons[k].append("short_long_pair")
                labelled[[i, j]] = True
                continue
        reasons[i].append("local_deviation")
        labelled[i] = True

    # Segments: a new one starts after each RR gap.
    unaccounted = np.r_[0.0, np.diff(e) / 1e6 - x[1:]]
    seg_start = unaccounted > cfg.rr_gap_tolerance_ms
    segment = np.cumsum(seg_start)

    # near_gap: close to any gap boundary or connection-related event.
    t = out["t"].to_numpy()
    marks = np.r_[gaps["start_elapsed_s"].to_numpy(dtype=float), gaps["end_elapsed_s"].to_numpy(dtype=float),
                  s.events.loc[s.events["type"].isin(BOUNDARY_EVENTS), "t"].to_numpy(dtype=float)]
    if len(marks):
        marks.sort()
        pos = np.searchsorted(marks, t)
        nearest = np.minimum(np.abs(t - marks[np.clip(pos - 1, 0, len(marks) - 1)]),
                             np.abs(marks[np.clip(pos, 0, len(marks) - 1)] - t))
        for i in np.flatnonzero(nearest <= cfg.rr_near_gap_s):
            reasons[i].append("near_gap")

    excluded = np.array([any(r in EXCLUDING for r in rs) for rs in reasons])
    out["reason"] = [";".join(rs) for rs in reasons]
    out["excluded"] = excluded
    out["segment"] = segment
    # adjacent_prev: this and the previous interval are both retained, were
    # adjacent in the raw series and belong to the same segment.
    out["adjacent_prev"] = np.r_[False, ~excluded[1:] & ~excluded[:-1] & (segment[1:] == segment[:-1])]
    return out


def cleaned(rr_flags: pd.DataFrame) -> pd.DataFrame:
    return rr_flags.loc[~rr_flags["excluded"]].reset_index(drop=True)


def successive_differences(rr_flags: pd.DataFrame) -> np.ndarray:
    """Differences RR[i] - RR[i-1] over valid adjacent retained pairs (ms)."""
    x = rr_flags["rr_ms"].to_numpy(dtype=float)
    ok = rr_flags["adjacent_prev"].to_numpy(dtype=bool)
    return (x[1:] - x[:-1])[ok[1:]]


def successive_pairs(rr_flags: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(RR[n], RR[n+1]) over valid adjacent retained pairs, for Poincaré."""
    x = rr_flags["rr_ms"].to_numpy(dtype=float)
    ok = rr_flags["adjacent_prev"].to_numpy(dtype=bool)
    return x[:-1][ok[1:]], x[1:][ok[1:]]


def summary(rr_flags: pd.DataFrame) -> dict:
    n = len(rr_flags)
    exc = int(rr_flags["excluded"].sum()) if n else 0
    counts: dict[str, int] = {}
    for rs in rr_flags["reason"] if n else []:
        for r in filter(None, rs.split(";")):
            counts[r] = counts.get(r, 0) + 1
    return {
        "intervals": n,
        "excluded": exc,
        "flagged_only": int(((rr_flags["reason"] != "") & ~rr_flags["excluded"]).sum()) if n else 0,
        "artifact_percentage": round(100 * exc / n, 3) if n else None,
        "reasons": dict(sorted(counts.items())),
        "segments": int(rr_flags["segment"].nunique()) if n else 0,
    }
