"""Command line entry points."""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .loader import LoadError
from .models import AnalysisConfig


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--output", default="analysis-output", help="output root directory (default: analysis-output)")
    p.add_argument("--config", help="JSON file overriding analysis parameters (see metrics.json -> analysis.config)")
    p.add_argument("--no-ecg", action="store_true", help="skip ECG analysis")
    p.add_argument("--no-plots", action="store_true", help="skip plots")
    p.add_argument("--rr-min", type=float, help="minimum plausible RR interval in ms (default 300)")
    p.add_argument("--rr-max", type=float, help="maximum plausible RR interval in ms (default 2000)")
    p.add_argument("--hrv-window", type=float, help="windowed HRV window in s (default 300)")
    p.add_argument("--hrv-step", type=float, help="windowed HRV step in s (default 30)")


def _config(a: argparse.Namespace) -> AnalysisConfig:
    over: dict = {}
    if a.config:
        over.update(json.loads(Path(a.config).read_text()))
        unknown = set(over) - {f.name for f in dataclasses.fields(AnalysisConfig)}
        if unknown:
            raise SystemExit(f"unknown config keys: {sorted(unknown)}")
    for flag, key in (("rr_min", "rr_min_ms"), ("rr_max", "rr_max_ms"), ("hrv_window", "hrv_window_s"),
                      ("hrv_step", "hrv_step_s")):
        if getattr(a, flag) is not None:
            over[key] = getattr(a, flag)
    if a.no_ecg:
        over["ecg_enabled"] = False
    if a.no_plots:
        over["plots"] = False
    return AnalysisConfig(**over)


def analyze_main(argv: list[str] | None = None) -> int:
    from .pipeline import analyze

    p = argparse.ArgumentParser(prog="analyze_session", description="Analyze h10 session directories.")
    p.add_argument("sessions", nargs="+", help="session directory (one or more)")
    _common(p)
    a = p.parse_args(argv)
    cfg = _config(a)
    code = 0
    for sess in a.sessions:
        try:
            res = analyze(sess, a.output, cfg)
        except (LoadError, ValueError) as e:
            print(f"{sess}: error: {e}", file=sys.stderr)
            code = 1
            continue
        s = res["summary"]
        q, h, v = s["quality"], s["heart_rate"], s["hrv"]

        def fmt(x, nd=1):
            return "n/a" if x is None else f"{x:.{nd}f}"
        print(f"{s['session_directory']}: quality={q['overall']}, duration {fmt(s['duration_seconds'])} s, "
              f"mean HR {fmt(h['mean_bpm'])} bpm, RR artifacts {fmt(q['rr_artifact_percentage'], 2)} %, "
              f"HRV {v['status']} (RMSSD {fmt(v.get('rmssd_ms'))} ms)\n  -> {res['output']}")
    return code


def compare_main(argv: list[str] | None = None) -> int:
    from .compare import compare

    p = argparse.ArgumentParser(prog="compare_sessions", description="Compare h10 sessions.")
    p.add_argument("sessions", nargs="+", help="session directories")
    _common(p)
    a = p.parse_args(argv)
    try:
        out = compare([Path(x) for x in a.sessions], Path(a.output), _config(a))
    except (LoadError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"comparison written to {out}")
    return 0
