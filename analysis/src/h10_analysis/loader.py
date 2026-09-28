"""Load an h10 session directory without modifying it.

The loader is strict about structure (required files and columns) and
lenient about content: malformed values are coerced to missing, reported as
issues, and rows that cannot be placed in time are excluded from analysis.
The files on disk are only ever opened for reading.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from .models import Issue, Session

SUPPORTED_SCHEMA_VERSIONS = {1}

COLUMNS = {
    "hr": {"timestamp": str, "elapsed_ns": "int64", "heart_rate_bpm": "float64"},
    "rr": {"timestamp": str, "elapsed_ns": "int64", "rr_ms": "float64"},
    "ecg": {"timestamp": str, "elapsed_ns": "int64", "sample_index": "int64", "ecg_uv": "float64"},
}

MAX_LISTED = 10  # row numbers listed per issue


class LoadError(Exception):
    """The session cannot be loaded (missing metadata, unreadable CSV structure)."""


def _read_csv(path: Path, stream: str, issues: list[Issue]) -> pd.DataFrame:
    cols = COLUMNS[stream]
    try:
        header = pd.read_csv(path, nrows=0).columns.tolist()
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError) as e:
        raise LoadError(f"{path.name}: cannot read CSV header: {e}") from e
    missing = [c for c in cols if c not in header]
    if missing:
        raise LoadError(f"{path.name}: missing required columns {missing}; found {header}")
    extra = [c for c in header if c not in cols]
    if extra:
        issues.append(Issue("info", f"{stream}.columns", f"{path.name} has extra columns {extra} (ignored)"))

    try:  # fast path: well-formed file
        df = pd.read_csv(path, usecols=list(cols), dtype=cols, engine="c")
    except (ValueError, pd.errors.ParserError):
        try:
            df = pd.read_csv(path, usecols=list(cols), dtype=str, engine="python", on_bad_lines="warn")
        except pd.errors.ParserError as e:
            raise LoadError(f"{path.name}: unreadable CSV: {e}") from e
        for c, dt in cols.items():
            if dt is str:
                continue
            num = pd.to_numeric(df[c], errors="coerce")
            bad = num.isna() & df[c].notna()
            if bad.any():
                rows = (np.flatnonzero(bad.to_numpy()) + 1).tolist()
                issues.append(Issue("error", f"{stream}.malformed_values",
                                    f"{path.name}: {len(rows)} non-numeric value(s) in column {c}",
                                    {"rows": rows[:MAX_LISTED]}))
            df[c] = num
    df.insert(0, "row", np.arange(1, len(df) + 1))  # 1-based data row number in the file

    # Timestamps: must be parseable and explicitly UTC.
    raw_ts = df["timestamp"].astype("string")
    ts = pd.to_datetime(raw_ts, format="ISO8601", utc=True, errors="coerce")
    unparsed = ts.isna()
    if unparsed.any():
        rows = df.loc[unparsed, "row"].tolist()
        issues.append(Issue("error", f"{stream}.timestamp_unparseable",
                            f"{path.name}: {len(rows)} missing or unparseable timestamp(s)",
                            {"rows": rows[:MAX_LISTED]}))
    not_utc = ~unparsed & ~(raw_ts.str.endswith("Z") | raw_ts.str.endswith("+00:00")).fillna(False)
    if not_utc.any():
        rows = df.loc[not_utc, "row"].tolist()
        issues.append(Issue("warning", f"{stream}.timestamp_not_utc",
                            f"{path.name}: {len(rows)} timestamp(s) not expressed in UTC (converted)",
                            {"rows": rows[:MAX_LISTED]}))
    df["ts"] = ts
    df = df.drop(columns="timestamp")

    # Rows without elapsed time cannot be placed on the time axis.
    no_time = df["elapsed_ns"].isna()
    if no_time.any():
        rows = df.loc[no_time, "row"].tolist()
        issues.append(Issue("error", f"{stream}.elapsed_missing",
                            f"{path.name}: {len(rows)} row(s) without elapsed_ns excluded from analysis",
                            {"rows": rows[:MAX_LISTED]}))
        df = df.loc[~no_time]
    df["elapsed_ns"] = df["elapsed_ns"].astype("int64")
    df["t"] = df["elapsed_ns"] / 1e9
    return df.reset_index(drop=True)


def _read_events(path: Path, issues: list[Issue]) -> pd.DataFrame:
    rows = []
    if path.exists():
        with path.open("r", encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    ev = json.loads(line)
                    rows.append({
                        "line": n,
                        "ts": pd.to_datetime(ev.get("timestamp"), format="ISO8601", utc=True, errors="coerce"),
                        "elapsed_ns": ev.get("elapsed_ns"),
                        "type": ev.get("type"),
                        "fields": {k: v for k, v in ev.items() if k not in ("timestamp", "elapsed_ns", "type")},
                    })
                except (json.JSONDecodeError, AttributeError) as e:
                    issues.append(Issue("error", "events.malformed", f"events.jsonl line {n}: {e}"))
    else:
        issues.append(Issue("warning", "events.missing", "events.jsonl not found"))
    df = pd.DataFrame(rows, columns=["line", "ts", "elapsed_ns", "type", "fields"])
    bad = df["elapsed_ns"].isna() | df["type"].isna()
    if bad.any():
        issues.append(Issue("error", "events.incomplete", f"{int(bad.sum())} event(s) without elapsed_ns or type",
                            {"lines": df.loc[bad, "line"].tolist()[:MAX_LISTED]}))
        df = df.loc[~bad]
    df["elapsed_ns"] = df["elapsed_ns"].astype("int64")
    df["t"] = df["elapsed_ns"] / 1e9
    return df.reset_index(drop=True)


def _count_raw(path: Path, issues: list[Issue]) -> dict[str, int] | None:
    """Count raw BLE frames by direction and characteristic name."""
    if not path.exists():
        return None
    counts: dict[str, int] = {"rx": 0, "tx": 0, "malformed": 0}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                counts["malformed"] += 1
                continue
            direction = d.get("direction", "rx")
            counts[direction] = counts.get(direction, 0) + 1
            key = f"{direction}:{d.get('name')}"
            counts[key] = counts.get(key, 0) + 1
    if counts["malformed"]:
        issues.append(Issue("warning", "raw.malformed", f"raw/ble.jsonl has {counts['malformed']} unreadable line(s)"))
    return counts


def load_session(path: str | Path, load_ecg: bool = True, count_raw: bool = True) -> Session:
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise LoadError(f"{path}: not a directory")
    meta_path = path / "metadata.json"
    if not meta_path.exists():
        raise LoadError(f"{path}: metadata.json not found (not an h10 session?)")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise LoadError(f"metadata.json: invalid JSON: {e}") from e

    version = metadata.get("schema_version")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise LoadError(f"unsupported schema_version {version!r}; supported: {sorted(SUPPORTED_SCHEMA_VERSIONS)}")

    issues: list[Issue] = []
    frames = {}
    for stream in ("hr", "rr", "ecg"):
        f = path / f"{stream}.csv"
        if stream == "ecg" and not load_ecg:
            frames[stream] = None
            continue
        if not f.exists():
            if stream == "ecg" and not (metadata.get("recording") or {}).get("ecg", True):
                frames[stream] = None
                continue
            raise LoadError(f"{f.name} not found")
        frames[stream] = _read_csv(f, stream, issues)

    started = pd.to_datetime(metadata.get("started_at"), format="ISO8601", utc=True, errors="coerce")
    if pd.isna(started):
        issues.append(Issue("error", "metadata.started_at", "started_at missing or unparseable"))
        started = None

    return Session(
        path=path,
        name=path.name,
        metadata=metadata,
        hr=frames["hr"],
        rr=frames["rr"],
        ecg=frames["ecg"],
        events=_read_events(path / "events.jsonl", issues),
        started_at=started,
        issues=issues,
        raw_counts=_count_raw(path / "raw" / "ble.jsonl", issues) if count_raw else None,
    )
