"""Recording gap detection per stream, correlated with events.

A gap is reported between the last sample before it and the first sample
after it. Nothing is interpolated.

- ECG: spacing > ecg_gap_factor x the measured sample period.
- RR: time between beats exceeds the interval itself by more than
  rr_gap_tolerance_ms (beats unaccounted for).
- HR: spacing > hr_gap_s (notifications arrive ~1 Hz).
- connection: from connection_lost to device_reconnected (or session end).
- leading/trailing: a stream starts later / ends earlier than the session by
  more than stream_edge_gap_s.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import AnalysisConfig, Session

GAP_COLUMNS = [
    "stream", "kind", "start_timestamp", "end_timestamp", "start_elapsed_s", "end_elapsed_s",
    "duration_ms", "expected_samples", "missing_samples", "related_event",
]

RELATED_EVENTS = {
    "connection_lost", "device_reconnected", "reconnect_attempt", "reconnect_failed", "data_stalled",
    "packets_dropped", "decode_error", "ecg_gap", "ecg_clock_rebased", "rr_chain_rebased",
    "sensor_contact_changed", "ecg_stream_started", "ecg_stream_stopped", "hr_stream_started",
}


def ecg_period_s(s: Session) -> float | None:
    """Measured median sample spacing, falling back to the metadata rate."""
    if s.ecg is not None and len(s.ecg) > 2:
        return float(np.median(np.diff(s.ecg["elapsed_ns"].to_numpy()))) / 1e9
    rate = s.ecg_sample_rate_hz
    return 1.0 / rate if rate else None


def _ts(s: Session, t: float) -> str:
    if s.started_at is None:
        return ""
    return (s.started_at + pd.Timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _row(s: Session, stream: str, kind: str, t0: float, t1: float, expected: int, missing: int) -> dict:
    return {
        "stream": stream, "kind": kind,
        "start_timestamp": _ts(s, t0), "end_timestamp": _ts(s, t1),
        "start_elapsed_s": round(t0, 6), "end_elapsed_s": round(t1, 6),
        "duration_ms": round((t1 - t0) * 1000, 3),
        "expected_samples": int(expected), "missing_samples": int(missing),
    }


def _internal(s: Session, stream: str, t: np.ndarray, spacing: float, threshold: float) -> list[dict]:
    rows = []
    d = np.diff(t)
    for i in np.flatnonzero(d > threshold):
        missing = max(int(round(d[i] / spacing)) - 1, 0)
        rows.append(_row(s, stream, "internal", t[i], t[i + 1], missing, missing))
    return rows


def _edges(s: Session, stream: str, t: np.ndarray, spacing: float, cfg: AnalysisConfig) -> list[dict]:
    rows = []
    dur = s.duration_s
    if len(t) == 0 or dur is None:
        return rows
    if t[0] > cfg.stream_edge_gap_s:
        n = int(t[0] / spacing)
        rows.append(_row(s, stream, "leading", 0.0, t[0], n, n))
    if dur - t[-1] > cfg.stream_edge_gap_s:
        n = int((dur - t[-1]) / spacing)
        rows.append(_row(s, stream, "trailing", t[-1], dur, n, n))
    return rows


def detect_gaps(s: Session, cfg: AnalysisConfig) -> pd.DataFrame:
    rows: list[dict] = []

    if s.ecg is not None and len(s.ecg) > 1:
        period = ecg_period_s(s)
        t = s.ecg["t"].to_numpy()
        rows += _internal(s, "ecg", t, period, cfg.ecg_gap_factor * period)
        rows += _edges(s, "ecg", t, period, cfg)

    if len(s.rr) > 1:
        t = s.rr["t"].to_numpy()
        rr_s = s.rr["rr_ms"].to_numpy() / 1000
        med = float(np.median(rr_s))
        # Time between beat i and i+1 should equal rr[i+1].
        unaccounted = np.diff(t) - rr_s[1:]
        for i in np.flatnonzero(unaccounted > cfg.rr_gap_tolerance_ms / 1000):
            missing = max(int(round(unaccounted[i] / med)), 1)
            rows.append(_row(s, "rr", "internal", t[i], t[i + 1], missing, missing))
        rows += _edges(s, "rr", t, med, cfg)

    if len(s.hr) > 1:
        t = s.hr["t"].to_numpy()
        spacing = float(np.median(np.diff(t)))
        rows += _internal(s, "hr", t, spacing, cfg.hr_gap_s)
        rows += _edges(s, "hr", t, spacing, cfg)

    rows += _connection_gaps(s)

    gaps = pd.DataFrame(rows, columns=[c for c in GAP_COLUMNS if c != "related_event"])
    gaps["related_event"] = [related_events(s, r.start_elapsed_s, r.end_elapsed_s, cfg) for r in gaps.itertuples()]
    return gaps.sort_values(["start_elapsed_s", "stream"], kind="stable").reset_index(drop=True)[GAP_COLUMNS]


def _connection_gaps(s: Session) -> list[dict]:
    rows = []
    ev = s.events.sort_values("elapsed_ns", kind="stable")
    lost = None
    for e in ev.itertuples():
        if e.type == "connection_lost" and lost is None:
            lost = e.t
        elif e.type == "device_reconnected" and lost is not None:
            rows.append(_row(s, "connection", "disconnected", lost, e.t, 0, 0))
            lost = None
    if lost is not None:
        end = s.duration_s if s.duration_s is not None else float(ev["t"].max())
        rows.append(_row(s, "connection", "disconnected_until_end", lost, end, 0, 0))
    return rows


def related_events(s: Session, t0: float, t1: float, cfg: AnalysisConfig) -> str:
    """Relevant events within [t0 - margin, t1 + margin], as 'type@HH:MM:SS.mmm; ...'."""
    m = cfg.event_match_margin_s
    ev = s.events
    sel = ev[(ev["t"] >= t0 - m) & (ev["t"] <= t1 + m) & ev["type"].isin(RELATED_EVENTS)]
    return "; ".join(f"{r.type}@{r.ts.strftime('%H:%M:%S.%f')[:-3] if not pd.isna(r.ts) else f'{r.t:.3f}s'}"
                     for r in sel.itertuples())
