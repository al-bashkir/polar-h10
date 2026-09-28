"""Synthetic h10 sessions with known ground truth, written in the exact h10
schema-v1 file format (see ../../docs/data-format.md)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

START = pd.Timestamp("2026-01-01T00:00:00Z")
FS = 129.9  # like a real H10 sensor clock
RR_DELAY_S = 1.5  # Polar RR timestamps lag the ECG like on a real device


def ts(elapsed_ns: np.ndarray) -> list[str]:
    t = START + pd.to_timedelta(np.asarray(elapsed_ns, dtype="int64"), unit="ns")
    return [x.strftime("%Y-%m-%dT%H:%M:%S.%f") + f"{x.nanosecond:03d}Z" for x in t]


@dataclass
class Truth:
    beat_t: np.ndarray  # true beat times (s)
    rr_ms: np.ndarray  # reported RR values (ms), aligned with rr.csv rows
    ecg_gap: tuple[float, float] | None = None
    artifacts: dict[str, int] = field(default_factory=dict)  # kind -> rr.csv row index (0-based)


def make_session(root: Path, name: str = "2026-01-01T00-00-00Z_TEST0001_abcd1234", duration: float = 120.0,
                 gap: tuple[float, float] | None = None, artifacts: bool = False, seed: int = 1,
                 hf_amp_ms: float = 30.0, base_rr_ms: float = 800.0) -> tuple[Path, Truth]:
    rng = np.random.default_rng(seed)
    d = root / name
    d.mkdir(parents=True)

    # True beats: base RR with a 0.25 Hz modulation and small noise.
    beats, t = [], 0.6
    while t < duration - 0.5:
        beats.append(t)
        t += (base_rr_ms + hf_amp_ms * np.sin(2 * np.pi * 0.25 * t) + rng.normal(0, 3)) / 1000
    beat_t = np.array(beats)

    # ECG: P-QRS-T-like waveform sampled at FS, int microvolts.
    et = np.arange(0.4, duration - 0.2, 1 / FS)
    v = np.full(len(et), -100.0) + rng.normal(0, 4, len(et))
    for b in beat_t:
        lo, hi = np.searchsorted(et, [b - 0.3, b + 0.5])
        x = et[lo:hi] - b
        v[lo:hi] += (1200 * np.exp(-(x / 0.008) ** 2) - 300 * np.exp(-((x - 0.025) / 0.01) ** 2)
                     + 200 * np.exp(-((x - 0.25) / 0.04) ** 2) + 60 * np.exp(-((x + 0.16) / 0.025) ** 2))
    ecg_keep = np.ones(len(et), dtype=bool)
    if gap:
        ecg_keep &= ~((et > gap[0]) & (et < gap[1]))

    # RR: interval ending at each beat, quantised to 1/1024 s like the sensor.
    raw = np.round(np.diff(beat_t) * 1024).astype(int)
    rr_ms = raw * 1000 / 1024
    rr_t = beat_t[1:] + RR_DELAY_S
    keep = np.ones(len(rr_ms), dtype=bool)
    if gap:  # beats during the disconnect are never reported
        keep &= ~((beat_t[1:] > gap[0]) & (beat_t[1:] < gap[1] + 0.2))
    rr_ms, rr_t = rr_ms[keep], rr_t[keep]
    truth = Truth(beat_t=beat_t, rr_ms=rr_ms.copy(), ecg_gap=gap)
    if artifacts:
        rr_ms = rr_ms.copy()
        # missed beat: merge two intervals; extra beat: split one; out of range value.
        i = 40
        rr_ms[i] = rr_ms[i] + rr_ms[i + 1]
        rr_ms, rr_t = np.delete(rr_ms, i + 1), np.delete(rr_t, i)  # merged interval ends at the later beat
        j = 70
        half = np.round(rr_ms[j] * 0.4 * 1.024) / 1.024
        rr_ms = np.insert(rr_ms, j, half)
        rr_ms[j + 1] -= half
        rr_t = np.insert(rr_t, j, rr_t[j] - (rr_ms[j + 1]) / 1000)
        k = 100
        rr_ms[k] = 250.0
        truth.artifacts = {"possible_missed_beat": i, "possible_extra_beat": j, "out_of_range": k}
        truth.rr_ms = rr_ms.copy()

    # HR: ~1 Hz notifications, sensor-like averaged value.
    hr_t = np.arange(1.0, duration - 0.3, 1.0)
    if gap:
        hr_t = hr_t[~((hr_t > gap[0]) & (hr_t < gap[1]))]
    hr_v = [int(round(60000 / np.mean(truth.rr_ms[max(0, np.searchsorted(rr_t, x) - 4):max(1, np.searchsorted(rr_t, x))])))
            for x in hr_t]

    def write_csv(fname: str, cols: dict) -> int:
        df = pd.DataFrame(cols)
        df.to_csv(d / fname, index=False)
        return len(df)

    ecg_ns = np.round(et[ecg_keep] * 1e9).astype("int64")
    n_ecg = write_csv("ecg.csv", {"timestamp": ts(ecg_ns), "elapsed_ns": ecg_ns,
                                  "sample_index": np.arange(len(ecg_ns)), "ecg_uv": np.round(v[ecg_keep]).astype(int)})
    rr_ns = np.round(rr_t * 1e9).astype("int64")
    n_rr = write_csv("rr.csv", {"timestamp": ts(rr_ns), "elapsed_ns": rr_ns,
                                "rr_ms": [f"{x:.10g}" for x in rr_ms]})
    hr_ns = np.round(hr_t * 1e9).astype("int64")
    n_hr = write_csv("hr.csv", {"timestamp": ts(hr_ns), "elapsed_ns": hr_ns, "heart_rate_bpm": hr_v})

    events = [(0.01, "session_started", {"name": "synthetic"}), (0.02, "device_connected", {"id": "TEST0001"}),
              (0.1, "hr_stream_started", {}), (0.2, "ecg_stream_started", {"sample_rate_hz": 130})]
    if gap:
        events += [(gap[0] + 0.5, "connection_lost", {"error": "device disconnected"}),
                   (gap[0] + 2.5, "reconnect_attempt", {"attempt": 1}),
                   (gap[1] - 0.3, "device_reconnected", {"attempt": 1}),
                   (gap[1] + 0.4, "ecg_gap", {"missing_samples_estimated": int((gap[1] - gap[0]) * 130)})]
    events += [(duration - 0.1, "session_stopped", {"reason": "duration"})]
    with (d / "events.jsonl").open("w") as f:
        for t_, typ, fields in events:
            ns = int(t_ * 1e9)
            f.write(json.dumps({"timestamp": ts([ns])[0], "elapsed_ns": ns, "type": typ, **fields}) + "\n")

    meta = {
        "schema_version": 1, "session_id": "01000000-0000-7000-8000-0000abcd1234", "name": "synthetic",
        "status": "completed", "stop_reason": "duration",
        "started_at": START.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "ended_at": (START + pd.Timedelta(seconds=duration)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "duration_seconds": duration, "timezone": "UTC", "utc_offset": "+00:00",
        "device": {"name": "Polar H10 TEST0001", "id": "TEST0001", "firmware": "5.0.0",
                   "battery_start_percent": 90, "battery_end_percent": 89},
        "recording": {"hr": True, "rr": True, "ecg": True, "ecg_sample_rate_hz": 130, "ecg_resolution_bits": 14,
                      "raw": False, "reconnect_attempts": 5},
        "statistics": {"hr_samples": n_hr, "rr_samples": n_rr, "ecg_samples": n_ecg, "ecg_frames": 0,
                       "ble_packets": 0, "decode_errors": 0, "dropped_packets": 0,
                       "ecg_gaps": 1 if gap else 0, "ecg_missing_samples_estimated": 0, "ecg_clock_rebases": 0,
                       "rr_chain_rebases": 0, "disconnects": 1 if gap else 0, "reconnections": 1 if gap else 0},
        "application": {"name": "h10", "version": "0.1.0", "go_version": "go1.27"},
    }
    (d / "metadata.json").write_text(json.dumps(meta, indent=2))
    return d, truth
