"""Dataset validation: metadata, counters, timestamps, ordering, sample indices.

Validation only reports; it never changes data. Every discrepancy becomes an
Issue so it appears in quality.json and the report.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import AnalysisConfig, Issue, Session

MAX_LISTED = 10
TS_TOLERANCE_NS = 1_000  # timestamp must equal started_at + elapsed_ns within 1 µs

KNOWN_EVENTS = {
    "session_started", "session_stopped", "device_connected", "connection_lost", "reconnect_attempt",
    "reconnect_failed", "device_reconnected", "hr_stream_started", "ecg_stream_started", "ecg_stream_stopped",
    "ecg_clock_anchored", "ecg_clock_rebased", "ecg_gap", "rr_chain_rebased", "sensor_contact_changed",
    "battery_level", "packets_dropped", "decode_error", "data_stalled", "write_error",
}


def validate(s: Session, cfg: AnalysisConfig) -> list[Issue]:
    issues = list(s.issues)
    issues += _metadata(s)
    issues += _counters(s)
    for stream in ("hr", "rr", "ecg"):
        df = getattr(s, stream)
        if df is not None:
            issues += _timestamps(s, stream, df)
    if s.ecg is not None:
        issues += _ecg_index(s.ecg)
    issues += _events(s)
    return issues


def _metadata(s: Session) -> list[Issue]:
    m = s.metadata
    out: list[Issue] = []
    for key in ("session_id", "started_at", "ended_at", "duration_seconds", "device", "recording", "statistics"):
        if m.get(key) is None:
            sev = "warning" if key in ("ended_at", "duration_seconds") else "error"
            out.append(Issue(sev, "metadata.missing_field", f"metadata.json: '{key}' is missing or null"))

    status = m.get("status")
    if status != "completed":
        out.append(Issue("warning", "metadata.status",
                         f"session status is '{status}' (stop reason: {m.get('stop_reason')}); "
                         + ("recording was never finalized" if status == "recording" else "recording ended abnormally"),
                         {"error": m.get("error")}))

    started = s.started_at
    ended = pd.to_datetime(m.get("ended_at"), format="ISO8601", utc=True, errors="coerce")
    dur = m.get("duration_seconds")
    if started is not None and not pd.isna(ended) and dur is not None:
        actual = (ended - started).total_seconds()
        if abs(actual - dur) > 1e-3:
            out.append(Issue("warning", "metadata.duration",
                             f"duration_seconds={dur} but ended_at - started_at = {actual:.6f} s"))
        if actual <= 0:
            out.append(Issue("error", "metadata.duration", "ended_at is not after started_at"))

    rec = m.get("recording") or {}
    if rec.get("ecg") and not rec.get("ecg_sample_rate_hz"):
        out.append(Issue("error", "metadata.ecg_sample_rate", "ECG recorded but ecg_sample_rate_hz missing"))

    stats = m.get("statistics") or {}
    for key, label in (("dropped_packets", "BLE packets dropped (queue full)"),
                       ("decode_errors", "undecodable BLE packets"),
                       ("ecg_gaps", "ECG gaps detected by the recorder"),
                       ("disconnects", "Bluetooth disconnects"),
                       ("ecg_clock_rebases", "ECG clock re-anchors"),
                       ("rr_chain_rebases", "RR chain re-anchors")):
        if stats.get(key):
            out.append(Issue("warning", f"metadata.{key}", f"recorder reported {stats[key]} {label}"))
    return out


def _counters(s: Session) -> list[Issue]:
    """Compare metadata counters with what is actually in the files."""
    stats = s.metadata.get("statistics") or {}
    out: list[Issue] = []
    pairs = [("hr_samples", s.hr), ("rr_samples", s.rr)]
    if s.ecg is not None:
        pairs.append(("ecg_samples", s.ecg))
    for key, df in pairs:
        if key not in stats:
            continue
        # Count rows in the file, including rows excluded for missing elapsed_ns.
        rows = int(df["row"].max()) if len(df) else 0
        if rows != stats[key]:
            out.append(Issue("error", "counters.mismatch",
                             f"metadata says {key} = {stats[key]}, file has {rows} rows",
                             {"metadata": stats[key], "actual": rows, "difference": rows - stats[key]}))
    if s.raw_counts is not None:
        raw = s.raw_counts
        for key, actual in (("ble_packets", raw.get("rx", 0)), ("ecg_frames", raw.get("rx:pmd_data", 0))):
            if key in stats and stats[key] != actual:
                out.append(Issue("warning", "counters.raw_mismatch",
                                 f"metadata says {key} = {stats[key]}, raw/ble.jsonl has {actual}",
                                 {"metadata": stats[key], "actual": actual}))
        # Undecodable frames are in raw but not in hr.csv, so equality only
        # holds without decode errors.
        hr_frames = raw.get("rx:hr_measurement", 0)
        if "hr_samples" in stats and not stats.get("decode_errors") and hr_frames != stats["hr_samples"]:
            out.append(Issue("warning", "counters.raw_mismatch",
                             f"raw/ble.jsonl has {hr_frames} HR frames, metadata hr_samples = {stats['hr_samples']}"))
    return out


def _rows(df: pd.DataFrame, mask) -> list[int]:
    return df.loc[mask, "row"].tolist()[:MAX_LISTED]


def _timestamps(s: Session, stream: str, df: pd.DataFrame) -> list[Issue]:
    out: list[Issue] = []
    if df.empty:
        out.append(Issue("warning", f"{stream}.empty", f"{stream}.csv has no data rows"))
        return out
    e = df["elapsed_ns"].to_numpy()
    d = np.diff(e)
    back = np.flatnonzero(d < 0) + 1
    dup = np.flatnonzero(d == 0) + 1
    if len(back):
        out.append(Issue("error", f"{stream}.elapsed_backwards",
                         f"{stream}: elapsed_ns goes backwards {len(back)} time(s)",
                         {"rows": df["row"].to_numpy()[back][:MAX_LISTED].tolist()}))
    if len(dup):
        out.append(Issue("error" if stream == "ecg" else "warning", f"{stream}.elapsed_duplicate",
                         f"{stream}: {len(dup)} duplicate elapsed_ns value(s)",
                         {"rows": df["row"].to_numpy()[dup][:MAX_LISTED].tolist()}))

    ts = df["ts"]
    valid = ts.notna().to_numpy()
    if valid.sum() > 1:
        tv = ts[valid].astype("int64").to_numpy()
        tb = np.flatnonzero(np.diff(tv) < 0)
        if len(tb):
            out.append(Issue("error", f"{stream}.timestamp_backwards",
                             f"{stream}: timestamp goes backwards {len(tb)} time(s)"))
    if s.started_at is not None and valid.any():
        expected = s.started_at.value + e[valid]
        off = np.abs(ts[valid].astype("int64").to_numpy() - expected)
        bad = off > TS_TOLERANCE_NS
        if bad.any():
            out.append(Issue("error", f"{stream}.timestamp_elapsed_mismatch",
                             f"{stream}: {int(bad.sum())} row(s) where timestamp != started_at + elapsed_ns",
                             {"rows": df["row"].to_numpy()[valid][bad][:MAX_LISTED].tolist(),
                              "max_difference_ns": int(off.max())}))

    dur = s.duration_s
    if dur is not None:
        lo, hi = -1e9, (dur + 1) * 1e9
        outside = (e < lo) | (e > hi)
        if outside.any():
            out.append(Issue("warning", f"{stream}.outside_session",
                             f"{stream}: {int(outside.sum())} row(s) outside the session time range",
                             {"rows": _rows(df, outside)}))
    return out


def _ecg_index(ecg: pd.DataFrame) -> list[Issue]:
    out: list[Issue] = []
    if ecg.empty:
        return out
    idx = ecg["sample_index"].to_numpy()
    if idx[0] != 0:
        out.append(Issue("warning", "ecg.index_start", f"first sample_index is {idx[0]}, expected 0"))
    d = np.diff(idx)
    dup = np.flatnonzero(d == 0) + 1
    back = np.flatnonzero(d < 0) + 1
    skip = np.flatnonzero(d > 1) + 1
    if len(dup):
        out.append(Issue("error", "ecg.index_duplicate", f"ecg: {len(dup)} duplicated sample_index value(s)",
                         {"rows": ecg["row"].to_numpy()[dup][:MAX_LISTED].tolist()}))
    if len(back):
        out.append(Issue("error", "ecg.index_backwards", f"ecg: sample_index decreases {len(back)} time(s)",
                         {"rows": ecg["row"].to_numpy()[back][:MAX_LISTED].tolist()}))
    if len(skip):
        out.append(Issue("error", "ecg.index_missing",
                         f"ecg: sample_index skips {int((d[d > 1] - 1).sum())} index value(s) at {len(skip)} place(s) "
                         "(rows missing from ecg.csv)",
                         {"rows": ecg["row"].to_numpy()[skip][:MAX_LISTED].tolist()}))
    return out


def _events(s: Session) -> list[Issue]:
    out: list[Issue] = []
    ev = s.events
    if ev.empty:
        return out
    unknown = sorted(set(ev["type"]) - KNOWN_EVENTS)
    if unknown:
        out.append(Issue("info", "events.unknown_types", f"unknown event types (ignored): {unknown}"))
    if (np.diff(ev["elapsed_ns"].to_numpy()) < 0).any():
        out.append(Issue("info", "events.order", "events are not in elapsed_ns order (sorted for analysis)"))
    stats = s.metadata.get("statistics") or {}
    counts = ev["type"].value_counts()
    for etype, key in (("connection_lost", "disconnects"), ("device_reconnected", "reconnections"),
                       ("ecg_gap", "ecg_gaps")):
        if key in stats and int(counts.get(etype, 0)) != stats[key]:
            out.append(Issue("warning", "events.counter_mismatch",
                             f"{int(counts.get(etype, 0))} '{etype}' event(s) but metadata {key} = {stats[key]}"))
    for etype in ("session_started", "session_stopped"):
        if etype not in counts:
            out.append(Issue("warning", "events.missing", f"no '{etype}' event"))
    return out


def has_errors(issues: list[Issue]) -> bool:
    return any(i.severity == "error" for i in issues)
