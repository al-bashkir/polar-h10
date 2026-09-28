"""Multi-session comparison: a table, a Markdown summary and trend plots.

Changes are described as measured values only; no health interpretation.
"""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from . import __version__  # noqa: E402
from .loader import load_session  # noqa: E402
from .models import AnalysisConfig  # noqa: E402
from .pipeline import analyze  # noqa: E402

COLUMNS = [
    ("started_at", "start (UTC)", None),
    ("session_name", "name", None),
    ("duration_seconds", "duration (s)", 1),
    ("quality", "data quality", None),
    ("mean_hr_bpm", "mean HR (bpm)", 1),
    ("min_hr_bpm", "min HR (bpm)", 0),
    ("max_hr_bpm", "max HR (bpm)", 0),
    ("rmssd_ms", "RMSSD (ms)", 1),
    ("sdnn_ms", "SDNN (ms)", 1),
    ("pnn50_percent", "pNN50 (%)", 1),
    ("rr_artifact_percentage", "RR artifacts (%)", 2),
    ("ecg_missing_samples", "ECG missing samples", 0),
]
TRENDS = [("mean_hr_bpm", "mean HR (bpm)"), ("rmssd_ms", "RMSSD (ms)"), ("sdnn_ms", "SDNN (ms)"),
          ("pnn50_percent", "pNN50 (%)"), ("rr_artifact_percentage", "RR artifacts (%)")]


def summary_for(session: Path, output_root: Path, cfg: AnalysisConfig) -> dict:
    """Reuse an existing analysis if it was made by this version with this config."""
    name = load_session(session, load_ecg=False, count_raw=False).name
    out = output_root / name
    try:
        metrics = json.loads((out / "metrics.json").read_text())
        a = metrics["analysis"]
        if a["version"] == __version__ and a["config_digest"] == cfg.digest():
            return json.loads((out / "summary.json").read_text())
    except (OSError, KeyError, json.JSONDecodeError):
        pass
    return analyze(session, output_root, cfg)["summary"]


def row(s: dict) -> dict:
    q, h, v = s["quality"], s["heart_rate"], s["hrv"]
    return {
        "session_directory": s["session_directory"], "started_at": s["started_at"], "session_name": s["session_name"],
        "duration_seconds": s["duration_seconds"], "quality": q["overall"],
        "mean_hr_bpm": h["mean_bpm"], "min_hr_bpm": h["min_bpm"], "max_hr_bpm": h["max_bpm"],
        "hrv_status": v["status"], "rmssd_ms": v.get("rmssd_ms"), "sdnn_ms": v.get("sdnn_ms"),
        "pnn50_percent": v.get("pnn50_percent"), "rr_artifact_percentage": q["rr_artifact_percentage"],
        "ecg_missing_samples": q["ecg_missing_samples"],
    }


def compare(sessions: list[Path], output_root: Path, cfg: AnalysisConfig) -> Path:
    rows = [row(summary_for(p, output_root, cfg)) for p in sessions]
    df = pd.DataFrame(rows).sort_values("started_at", kind="stable").reset_index(drop=True)
    out = output_root / "comparison"
    out.mkdir(parents=True, exist_ok=True)
    df.to_csv(out / "comparison.csv", index=False, float_format="%.6f")

    L = ["# Session comparison\n",
         "Values are measured metrics per session. A change in a metric describes a measured difference and does "
         "not by itself indicate a change in health. HRV metrics depend on recording duration, posture, activity and "
         "data quality; compare like with like.\n",
         "| " + " | ".join(lab for _, lab, _ in COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for r in df.itertuples():
        cells = []
        for key, _, nd in COLUMNS:
            val = getattr(r, key)
            cells.append("n/a" if val is None or (isinstance(val, float) and pd.isna(val))
                         else f"{val:.{nd}f}" if nd is not None and isinstance(val, (int, float)) else str(val))
        L.append("| " + " | ".join(cells) + " |")
    L.append("")
    not_ok = df[df["hrv_status"] != "ok"]
    if len(not_ok):
        pairs = zip(not_ok["session_directory"], not_ok["hrv_status"], strict=True)
        L.append("HRV not calculated (insufficient or poor data) for: "
                 + ", ".join(f"`{d}` ({s})" for d, s in pairs) + ".\n")
    for key, lab in TRENDS:
        vals = [(r.started_at, getattr(r, key)) for r in df.itertuples() if getattr(r, key) is not None
                and not pd.isna(getattr(r, key))]
        if vals:
            L.append(f"**{lab}**\n")
            L += [f"- {t}: {v:.2f}" for t, v in vals]
            L.append("")
    if len(df) > 1:
        _trend_plot(df, out / "trends.png")
        L.append("![trends](trends.png)\n")
    (out / "comparison.md").write_text("\n".join(L), encoding="utf-8")
    return out


def _trend_plot(df: pd.DataFrame, path: Path) -> None:
    x = pd.to_datetime(df["started_at"], format="ISO8601", utc=True)
    fig, axes = plt.subplots(len(TRENDS), 1, figsize=(10, 2.2 * len(TRENDS)), sharex=True)
    for ax, (key, lab) in zip(axes, TRENDS, strict=True):
        y = pd.to_numeric(df[key], errors="coerce")
        ax.plot(x, y, "o-", ms=4)
        ax.set_ylabel(lab, fontsize=8)
        ax.grid(alpha=0.3)
    axes[0].set_title("Per-session metrics (missing points: metric not calculated)")
    axes[-1].set_xlabel("session start (UTC)")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=110, metadata={"Software": None})
    plt.close(fig)
