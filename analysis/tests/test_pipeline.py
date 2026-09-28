import hashlib
import json

import pandas as pd
import pytest

from h10_analysis.cli import analyze_main, compare_main
from h10_analysis.models import AnalysisConfig
from h10_analysis.pipeline import analyze, jsonable
from synth import make_session

FORBIDDEN = ["healthy", "arrhythmia", "afib", "atrial fibrillation", "diagnos", "normal ecg", "disease"]


def digest_tree(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def long_session(tmp_path_factory):
    root = tmp_path_factory.mktemp("sessions")
    d, truth = make_session(root, duration=420.0, gap=(200.0, 206.0), artifacts=True)
    return d, truth


def test_end_to_end(long_session, tmp_path):
    d, truth = long_session
    before = digest_tree(d)
    res = analyze(d, tmp_path / "out", AnalysisConfig())
    assert digest_tree(d) == before, "input files must not change"

    out = tmp_path / "out" / d.name
    for f in ("summary.json", "quality.json", "metrics.json", "report.md", "cleaned_rr.csv", "artifacts_rr.csv",
              "gaps.csv", "windowed_hrv.csv", "rpeaks.csv", "rr_ecg_comparison.csv"):
        assert (out / f).exists(), f
    for p in ("heart-rate", "rr", "rr-cleaned", "ecg-overview", "ecg-segment", "poincare", "rr-histogram",
              "hrv-windowed", "ecg-review-1"):
        assert (out / "plots" / f"{p}.png").exists(), p

    summary = json.loads((out / "summary.json").read_text())
    assert summary == res["summary"]
    assert summary["summary_schema_version"] == 1
    assert summary["quality"]["overall"] == "usable_with_caution"  # gap + disconnect
    assert summary["hrv"]["status"] == "ok" and summary["hrv"]["rmssd_ms"] > 0

    # All three injected artifacts are excluded with the expected reason.
    art = pd.read_csv(out / "artifacts_rr.csv")
    for reason, idx in truth.artifacts.items():
        row = art[art["source_row"] == idx + 1]
        assert len(row) == 1 and reason in row["reason"].iloc[0] and row["action"].iloc[0] == "excluded", reason
    cleaned = pd.read_csv(out / "cleaned_rr.csv")
    assert not set(cleaned["source_row"]) & {i + 1 for i in truth.artifacts.values()}
    # A new segment starts at the disconnect (Polar RR times lag by ~1.5 s).
    # (The synthetic out-of-range value keeps its original timestamp spacing,
    # which also starts a segment; real h10 RR timestamps are a cumulative
    # chain and cannot produce that.)
    t = pd.to_datetime(cleaned["timestamp"])
    starts = (t[cleaned["segment"].diff() > 0] - t.iloc[0]).dt.total_seconds() + cleaned["elapsed_ns"].iloc[0] / 1e9
    assert any(206 < x < 209 for x in starts)

    gaps = pd.read_csv(out / "gaps.csv")
    ecg_gap = gaps[gaps["stream"] == "ecg"].iloc[0]
    assert "connection_lost" in ecg_gap["related_event"]
    assert list(gaps.columns[:3]) == ["stream", "kind", "start_timestamp"]

    metrics = json.loads((out / "metrics.json").read_text())
    assert metrics["analysis"]["config"]["rr_min_ms"] == 300
    assert set(metrics["analysis"]["dependencies"]) == {"numpy", "pandas", "scipy", "matplotlib"}
    assert metrics["hrv"]["windowed"]["valid_windows"] > 0
    assert metrics["hrv"]["frequency_domain"]["status"] == "insufficient_data"  # longest segment ~200 s
    cmp_ = metrics["ecg"]["rr_comparison"]
    # The injected artifacts are not in the ECG: they show up as disagreements.
    assert cmp_["mismatched"] + cmp_["unmatched_polar_beats"] >= 3

    q = json.loads((out / "quality.json").read_text())
    assert q["rr"]["excluded_not_reproduced_by_ecg"] >= 3
    assert q["classification"]["scope"].startswith("recording and data quality only")

    report = (out / "report.md").read_text()
    for section in ("## Session", "## Data integrity", "## Recording quality", "## Heart rate", "## RR intervals",
                    "## HRV", "## ECG", "## Artifacts", "## Connection events", "## Notable observations",
                    "## Limitations"):
        assert section in report
    low = report.lower()
    assert not [w for w in FORBIDDEN if w in low.replace("not a medical assessment", "")]


def test_reproducible(long_session, tmp_path):
    d, _ = long_session
    analyze(d, tmp_path / "a")
    analyze(d, tmp_path / "b")
    assert digest_tree(tmp_path / "a") == digest_tree(tmp_path / "b")


def test_refuses_output_inside_session(long_session):
    d, _ = long_session
    with pytest.raises(ValueError, match="inside the session"):
        analyze(d, d / "analysis")


def test_refuses_to_replace_foreign_directory(long_session, tmp_path):
    d, _ = long_session
    (tmp_path / d.name).mkdir()
    (tmp_path / d.name / "notes.txt").write_text("mine")
    with pytest.raises(ValueError, match="not overwriting"):
        analyze(d, tmp_path)


def test_short_session_reports_insufficient_hrv(tmp_path):
    d, _ = make_session(tmp_path / "s", duration=30.0)
    res = analyze(d, tmp_path / "out", AnalysisConfig(plots=False))
    assert res["summary"]["hrv"]["status"] == "insufficient_data"
    assert res["summary"]["hrv"]["rmssd_ms"] is None
    assert res["summary"]["quality"]["overall"] == "good"


def test_jsonable():
    import numpy as np
    assert jsonable({"a": np.float64("nan"), "b": np.int64(3), "c": [np.float32(1.23456789)], "d": np.bool_(True)}) \
        == {"a": None, "b": 3, "c": [1.234568], "d": True}


def test_cli_and_compare(tmp_path, capsys):
    a, _ = make_session(tmp_path / "s", name="2026-01-01T00-00-00Z_TEST0001_aaaa0001", duration=180, seed=1)
    b, _ = make_session(tmp_path / "s", name="2026-01-02T00-00-00Z_TEST0001_aaaa0002", duration=180, seed=2,
                        hf_amp_ms=45)
    m = json.loads((b / "metadata.json").read_text())
    m["started_at"] = "2026-01-02T00:00:00.000000Z"  # keep metadata consistent with the later session
    m["ended_at"] = "2026-01-02T00:03:00.000000Z"
    (b / "metadata.json").write_text(json.dumps(m))
    out = tmp_path / "out"
    assert analyze_main([str(a), "--output", str(out), "--no-plots", "--rr-min", "280"]) == 0
    assert json.loads((out / a.name / "metrics.json").read_text())["analysis"]["config"]["rr_min_ms"] == 280
    assert compare_main([str(a), str(b), "--output", str(out), "--no-plots"]) == 0
    comp = pd.read_csv(out / "comparison" / "comparison.csv")
    assert list(comp["session_directory"]) == [a.name, b.name]
    md = (out / "comparison" / "comparison.md").read_text()
    assert "**RMSSD (ms)**" in md and "does not by itself indicate a change in health" in md
    assert (out / "comparison" / "trends.png").exists()
    assert analyze_main([str(tmp_path / "missing")]) == 1
