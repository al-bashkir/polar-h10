"""End-to-end analysis of one session: load -> validate -> gaps -> quality ->
artifacts -> cleaned data -> metrics -> plots -> report."""

from __future__ import annotations

import json
import math
import platform
import shutil
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from . import __version__, crosscheck, ecg, hr, hrv, plots, quality, report, rr
from . import gaps as gapmod
from .loader import load_session
from .models import AnalysisConfig, Session
from .validation import validate

SUMMARY_SCHEMA_VERSION = 1
OUTPUT_MARKER = "summary.json"  # an existing output dir must contain this to be replaced
TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def versions() -> dict:
    deps = {}
    for pkg in ("numpy", "pandas", "scipy", "matplotlib"):
        try:
            deps[pkg] = importlib_metadata.version(pkg)
        except importlib_metadata.PackageNotFoundError:
            deps[pkg] = None
    return {"name": "h10-analysis", "version": __version__, "python": platform.python_version(), "dependencies": deps}


def jsonable(x: Any) -> Any:
    """Convert to JSON-safe values: numpy scalars, NaN -> null, floats rounded to 6 decimals."""
    if isinstance(x, dict):
        return {str(k): jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else round(f, 6)
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, pd.Timestamp):
        return x.strftime(TS_FORMAT)
    return x


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(jsonable(obj), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _timestamps(s: Session, elapsed_ns: pd.Series | np.ndarray) -> list[str]:
    if s.started_at is None:
        return [""] * len(elapsed_ns)
    ts = s.started_at + pd.to_timedelta(np.asarray(elapsed_ns, dtype="int64"), unit="ns")
    return [t.isoformat().replace("+00:00", "Z") for t in ts]


def prepare_output(session: Path, output_root: Path, name: str) -> Path:
    out = (output_root / name).resolve()
    session = session.resolve()
    if out == session or session in out.parents:
        raise ValueError(f"refusing to write analysis output inside the session directory {session}")
    if out.exists():
        if not (out / OUTPUT_MARKER).exists() and any(out.iterdir()):
            raise ValueError(f"{out} exists and does not look like an analysis output; not overwriting")
        shutil.rmtree(out)  # previous analysis of this session: replace completely
    (out / "plots").mkdir(parents=True)
    return out


def analyze(session_dir: str | Path, output_root: str | Path = "analysis-output",
            cfg: AnalysisConfig | None = None) -> dict:
    cfg = cfg or AnalysisConfig()
    # 1. load
    s = load_session(session_dir, load_ecg=cfg.ecg_enabled)
    out = prepare_output(s.path, Path(output_root).expanduser(), s.name)
    # 2. validate
    issues = validate(s, cfg)
    # 3. gaps
    gaps = gapmod.detect_gaps(s, cfg)
    # 4-6. RR artifacts and cleaned series
    rr_flags = rr.analyze_rr(s, gaps, cfg)
    rr_summary = rr.summary(rr_flags)

    # ECG integrity, filtering, R-peaks, RR comparison
    ecg_res: dict | None = None
    comp = None
    rpeaks = None
    filt = None
    if cfg.ecg_enabled and s.ecg is not None and len(s.ecg) > 1:
        integ = ecg.integrity(s, gaps, cfg)
        seg = ecg.segments(s.ecg, gaps)
        fs = integ["measured_sample_rate_hz"]
        filt = ecg.filtered(s.ecg, seg, fs, cfg)
        ecg_res = {"integrity": integ, "filter": ecg.filter_description(fs, cfg)}
        if cfg.rpeak_enabled:
            rpeaks = ecg.detect_rpeaks(s.ecg, filt, seg, fs)
            comp, comp_summary = ecg.compare_rr(rr_flags, rpeaks, ecg.spans(s.ecg, seg), cfg)
            ecg_res["rpeaks"] = {"count": int(len(rpeaks)), "detector": "see h10_analysis.ecg module docstring"}
            ecg_res["rr_comparison"] = comp_summary
            if len(comp):
                covered = comp["status"] != "outside_ecg"
                rr_flags["t_ecg"] = np.where(covered, comp["t"] + comp["offset_ms"] / 1000, np.nan)
                rr_flags["ecg_check"] = comp["status"].to_numpy()

    # 7. metrics
    hr_stats = hr.heart_rate_stats(s)
    hrv_session = hrv.session_hrv(rr_flags, cfg)
    windows = hrv.windowed_hrv(rr_flags, cfg, s.started_at)
    freq = hrv.frequency_domain(rr_flags, cfg)
    hr_rr_df, hr_rr = crosscheck.hr_vs_rr(s, rr_flags, gaps, cfg)
    q = quality.build_quality(s, issues, gaps, rr_summary, rr_flags, ecg_res["integrity"] if ecg_res else None,
                              ecg_res.get("rr_comparison") if ecg_res else None, hr_rr)
    regions = quality.ecg_review_regions(ecg_res["integrity"] if ecg_res else None, gaps, rr_flags,
                                         ecg_res.get("rr_comparison") if ecg_res else None)

    # Data outputs
    _write_rr(out, s, rr_flags)
    gaps.to_csv(out / "gaps.csv", index=False)
    windows.to_csv(out / "windowed_hrv.csv", index=False, float_format="%.6f")
    hr_rr_df.to_csv(out / "hr_rr_crosscheck.csv", index=False, float_format="%.6f")
    if rpeaks is not None:
        pd.DataFrame({"sample_index": rpeaks["sample_index"], "timestamp": _timestamps(s, rpeaks["elapsed_ns"]),
                      "elapsed_ns": rpeaks["elapsed_ns"], "ecg_uv": rpeaks["ecg_uv"],
                      "filtered_uv": rpeaks["filtered_uv"], "segment": rpeaks["segment"]}
                     ).to_csv(out / "rpeaks.csv", index=False, float_format="%.3f")
    if comp is not None:
        comp.to_csv(out / "rr_ecg_comparison.csv", index=False, float_format="%.6f")

    w_ok = windows[windows["status"] == "ok"]
    metrics = {
        "analysis": {**versions(), "config": cfg.to_dict(), "config_digest": cfg.digest()},
        "session": _session_info(s),
        "heart_rate": hr_stats,
        "rr": rr_summary,
        "hrv": {
            "session": hrv_session,
            "frequency_domain": freq,
            "windowed": {"window_s": cfg.hrv_window_s, "step_s": cfg.hrv_step_s, "windows": int(len(windows)),
                         "valid_windows": int(len(w_ok)),
                         "rejected": windows.loc[windows["status"] != "ok", "status"].value_counts().to_dict(),
                         "rmssd_ms_range": [w_ok["rmssd_ms"].min(), w_ok["rmssd_ms"].max()] if len(w_ok) else None,
                         "mean_hr_bpm_range": [w_ok["mean_hr_bpm"].min(), w_ok["mean_hr_bpm"].max()] if len(w_ok) else None},
        },
        "ecg": ecg_res,
        "cross_checks": q["cross_checks"],
        "review_regions": regions,
    }
    summary = build_summary(s, q, hr_stats, hrv_session, freq, ecg_res)

    # 8. plots
    plot_files: list[str] = []
    if cfg.plots:
        plot_files = _plots(out / "plots", s, rr_flags, gaps, windows, hrv_session, rpeaks, filt, comp, regions)
    metrics["plots"] = plot_files

    write_json(out / "quality.json", q)
    write_json(out / "metrics.json", metrics)
    write_json(out / "summary.json", summary)
    # 9. report
    (out / "report.md").write_text(report.render(s, q, metrics, summary, gaps, rr_flags, plot_files), encoding="utf-8")
    return {"output": str(out), "summary": jsonable(summary)}


def _session_info(s: Session) -> dict:
    m = s.metadata
    return {
        "directory": s.name,
        "session_id": m.get("session_id"),
        "name": m.get("name"),
        "schema_version": m.get("schema_version"),
        "started_at": m.get("started_at"),
        "ended_at": m.get("ended_at"),
        "duration_seconds": m.get("duration_seconds"),
        "timezone": m.get("timezone"),
        "status": m.get("status"),
        "stop_reason": m.get("stop_reason"),
        "device": {k: (m.get("device") or {}).get(k) for k in ("name", "id", "firmware", "battery_start_percent",
                                                                 "battery_end_percent")},
        "recording": m.get("recording"),
        "recorder": m.get("application"),
    }


def build_summary(s: Session, q: dict, hr_stats: dict, hrv_session: dict, freq: dict, ecg_res: dict | None) -> dict:
    td = hrv_session.get("time_domain", {})
    pc = hrv_session.get("poincare", {})
    comp = (ecg_res or {}).get("rr_comparison") or {}
    return {
        "summary_schema_version": SUMMARY_SCHEMA_VERSION,
        "analysis_version": __version__,
        "session_id": s.metadata.get("session_id"),
        "session_directory": s.name,
        "session_name": s.metadata.get("name"),
        "device_id": (s.metadata.get("device") or {}).get("id"),
        "started_at": s.metadata.get("started_at"),
        "duration_seconds": s.duration_s,
        "quality": {
            "overall": q["classification"]["overall"],
            "reasons": q["classification"]["reasons"],
            "validation_errors": q["validation"]["error"],
            "validation_warnings": q["validation"]["warning"],
            "rr_artifact_percentage": q["rr"]["artifact_percentage"],
            "rr_gaps": q["rr"]["gaps"],
            "hr_gaps": q["hr"]["gaps"],
            "ecg_gaps": (q.get("ecg") or {}).get("gaps"),
            "ecg_missing_samples": (q.get("ecg") or {}).get("missing_samples"),
            "disconnects": q["connection"]["disconnects"],
        },
        "heart_rate": {k: hr_stats.get(k) for k in ("mean_bpm", "median_bpm", "min_bpm", "max_bpm", "std_bpm")},
        "hrv": {
            "status": hrv_session["status"],
            **{k: td.get(k) for k in ("mean_rr_ms", "mean_hr_bpm", "rmssd_ms", "sdnn_ms", "pnn50_percent",
                                      "nn_count", "valid_duration_s")},
            "sd1_ms": pc.get("sd1_ms"), "sd2_ms": pc.get("sd2_ms"),
            "frequency_status": freq["status"],
            "lf_ms2": freq.get("lf_ms2"), "hf_ms2": freq.get("hf_ms2"), "lf_hf_ratio": freq.get("lf_hf_ratio"),
        },
        "ecg_rr_agreement": {k: comp.get(k) for k in ("compared", "mismatched", "mean_absolute_error_ms",
                                                      "median_timestamp_offset_ms")} if comp else None,
    }


def _write_rr(out: Path, s: Session, f: pd.DataFrame) -> None:
    clean = f.loc[~f["excluded"]]
    pd.DataFrame({
        "timestamp": _timestamps(s, clean["elapsed_ns"]), "elapsed_ns": clean["elapsed_ns"],
        "rr_ms": clean["rr_ms"], "segment": clean["segment"], "source_row": clean["source_row"],
        "successive_difference_valid": clean["adjacent_prev"], "review_flag": clean["reason"],
    }).to_csv(out / "cleaned_rr.csv", index=False)
    art = f.loc[f["reason"] != ""]
    pd.DataFrame({
        "timestamp": _timestamps(s, art["elapsed_ns"]), "elapsed_ns": art["elapsed_ns"],
        "source_row": art["source_row"], "rr_ms": art["rr_ms"], "local_median_ms": art["local_median_ms"],
        "reason": art["reason"], "action": np.where(art["excluded"], "excluded", "flagged"),
    }).to_csv(out / "artifacts_rr.csv", index=False)


def _plots(pdir: Path, s, rr_flags, gaps, windows, hrv_session, rpeaks, filt, comp, regions) -> list[str]:
    dur = s.duration_s or float(max(s.hr["t"].max() if len(s.hr) else 0, rr_flags["t"].max() if len(rr_flags) else 0))
    names = []
    if len(s.hr):
        names.append(plots.heart_rate(pdir, s.hr, rr_flags, gaps, dur))
    if len(rr_flags):
        names += [plots.rr_raw(pdir, rr_flags, gaps, dur), plots.rr_cleaned(pdir, rr_flags, gaps, dur),
                  plots.histogram(pdir, rr_flags), plots.poincare(pdir, rr_flags, hrv_session.get("poincare"))]
    w = plots.windowed(pdir, windows, dur)
    if w:
        names.append(w)
    if s.ecg is not None and len(s.ecg) > 1 and filt is not None:
        names.append(plots.ecg_overview(pdir, s.ecg, gaps, regions))
        pk = rpeaks if rpeaks is not None else pd.DataFrame(columns=["t", "ecg_uv"])
        names += plots.ecg_segments(pdir, s.ecg, pk, _clean_start(s, regions), filt)
        polar_t = rr_flags["t_ecg"].dropna().to_numpy() if "t_ecg" in rr_flags else None
        exc_t = rr_flags.loc[rr_flags["excluded"], "t_ecg"].dropna().to_numpy() if "t_ecg" in rr_flags else None
        names += plots.ecg_review(pdir, s.ecg, pk, regions, polar_t, exc_t)
        if comp is not None:
            c = plots.rr_comparison(pdir, comp)
            if c:
                names.append(c)
    return names


def _clean_start(s: Session, regions: list[dict], length: float = 60.0) -> float:
    """Start of a representative ECG window: the first 60 s (or shorter) window
    after the first 5 s that overlaps no review region; else the start."""
    t0, t1 = float(s.ecg["t"].iloc[0]), float(s.ecg["t"].iloc[-1])
    length = min(length, t1 - t0)
    for start in np.arange(t0 + min(5.0, max(t1 - t0 - length, 0)), t1 - length + 1e-9, 5.0):
        if not any(r["start_s"] < start + length and r["end_s"] > start for r in regions):
            return float(start)
    return t0
