"""Configuration and small shared data structures."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class AnalysisConfig:
    """All tunable parameters. Stored verbatim in every output (metrics.json).

    Thresholds are data-quality heuristics, not medical criteria.
    """

    # RR plausibility and artifact detection
    rr_min_ms: float = 300.0
    rr_max_ms: float = 2000.0
    rr_local_window: int = 11  # beats, centred rolling median (odd)
    rr_local_threshold: float = 0.25  # |rr - local median| / local median
    rr_pattern_tolerance: float = 0.15  # tolerance for missed/extra beat patterns
    rr_gap_tolerance_ms: float = 250.0  # time between beats minus rr_ms
    rr_near_gap_s: float = 2.0  # flag beats this close to a gap/connection event

    # Gap detection
    hr_gap_s: float = 2.5  # HR notifications arrive ~1 Hz
    ecg_gap_factor: float = 1.5  # gap if spacing > factor * sample period
    stream_edge_gap_s: float = 5.0  # stream starts late / ends early by more than this
    event_match_margin_s: float = 3.0  # events within this margin relate to a gap

    # HRV
    hrv_min_duration_s: float = 60.0  # of valid NN intervals
    hrv_min_intervals: int = 50
    hrv_max_artifact_pct: float = 20.0
    hrv_window_s: float = 300.0
    hrv_step_s: float = 30.0
    hrv_window_min_coverage: float = 0.8  # valid NN time / window length
    hrv_window_max_artifact_pct: float = 10.0
    freq_enabled: bool = True
    freq_min_segment_s: float = 300.0
    freq_resample_hz: float = 4.0
    freq_max_artifact_pct: float = 5.0

    # ECG
    ecg_enabled: bool = True
    ecg_highpass_hz: float = 0.5
    ecg_lowpass_hz: float = 40.0
    ecg_filter_order: int = 2
    ecg_flat_s: float = 1.0  # identical consecutive values for this long
    ecg_abs_limit_uv: float = 10_000.0  # |ecg| beyond this is implausible for chest ECG
    rpeak_enabled: bool = True
    rpeak_match_tolerance_ms: float = 150.0
    rr_mismatch_ms: float = 20.0  # |ECG RR - Polar RR| above this is a mismatch
    rr_align_search_s: float = 5.0  # Polar RR timestamp offset search range

    # Cross-validation
    hr_disagreement_bpm: float = 10.0
    hr_disagreement_min_s: float = 5.0

    # Output
    plots: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    def digest(self) -> str:
        blob = json.dumps(self.to_dict(), sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest()[:16]


SEVERITIES = ("error", "warning", "info")


@dataclass
class Issue:
    """A validation finding. error = data cannot be trusted as-is;
    warning = inconsistency or loss that limits interpretation; info = note."""

    severity: str
    check: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "check": self.check, "message": self.message, "details": self.details}


@dataclass
class Session:
    """A loaded session. Frames are read-only views of the input files.

    Time axis: ``elapsed_ns`` (int64, host monotonic clock). ``t`` is the same
    in seconds (float) for convenience. ``ts`` is the parsed UTC timestamp.
    """

    path: Path
    name: str  # directory name, used as the output directory name
    metadata: dict[str, Any]
    hr: pd.DataFrame
    rr: pd.DataFrame
    ecg: pd.DataFrame | None
    events: pd.DataFrame
    started_at: pd.Timestamp | None
    issues: list[Issue] = field(default_factory=list)  # found while loading
    raw_counts: dict[str, int] | None = None  # from raw/ble.jsonl

    @property
    def ecg_sample_rate_hz(self) -> float | None:
        rate = (self.metadata.get("recording") or {}).get("ecg_sample_rate_hz")
        return float(rate) if rate else None

    @property
    def duration_s(self) -> float | None:
        d = self.metadata.get("duration_seconds")
        return float(d) if d is not None else None
