import numpy as np
import pandas as pd

from h10_analysis import ecg, rr
from h10_analysis import gaps as gapmod
from h10_analysis.loader import load_session
from h10_analysis.models import AnalysisConfig
from synth import RR_DELAY_S, make_session

CFG = AnalysisConfig()


def _run(d):
    s = load_session(d)
    g = gapmod.detect_gaps(s, CFG)
    integ = ecg.integrity(s, g, CFG)
    seg = ecg.segments(s.ecg, g)
    fs = integ["measured_sample_rate_hz"]
    raw_before = s.ecg["ecg_uv"].to_numpy().copy()
    filt = ecg.filtered(s.ecg, seg, fs, CFG)
    pk = ecg.detect_rpeaks(s.ecg, filt, seg, fs)
    assert np.array_equal(s.ecg["ecg_uv"].to_numpy(), raw_before), "filtering must not modify raw ECG"
    return s, g, integ, seg, pk


def test_integrity_clean(tmp_path):
    d, _ = make_session(tmp_path)
    s, g, integ, seg, pk = _run(d)
    assert integ["continuous"] and integ["missing_samples"] == 0 and integ["gaps"] == 0
    assert abs(integ["measured_sample_rate_hz"] - 129.9) < 0.01
    assert integ["expected_period_ms"] == 1000 / 130
    assert integ["flatline_regions"] == [] and integ["extreme_value_samples"] == 0


def test_rpeaks_match_true_beats(tmp_path):
    d, truth = make_session(tmp_path)
    s, g, integ, seg, pk = _run(d)
    t0, t1 = s.ecg["t"].iloc[0], s.ecg["t"].iloc[-1]
    true = truth.beat_t[(truth.beat_t > t0 + 0.1) & (truth.beat_t < t1 - 0.1)]
    assert len(pk) == len(true)
    # Sub-sample interpolation: within 2 ms of the true R time.
    err = pk["t"].to_numpy() - true
    assert np.abs(err).max() < 0.002


def test_ecg_gap_splits_segments_and_peaks(tmp_path):
    d, truth = make_session(tmp_path, gap=(40.0, 45.0))
    s, g, integ, seg, pk = _run(d)
    assert integ["gaps"] == 1 and not integ["continuous"]
    assert seg.max() == 1
    ecg_rr = ecg.ecg_rr(pk)
    # No ECG-derived interval spans the gap.
    assert ecg_rr["rr_ms"].max() < 1000


def test_polar_rr_vs_ecg_alignment(tmp_path):
    d, truth = make_session(tmp_path, gap=(40.0, 45.0))
    s, g, integ, seg, pk = _run(d)
    f = rr.analyze_rr(s, g, CFG)
    comp, summ = ecg.compare_rr(f, pk, ecg.spans(s.ecg, seg), CFG)
    # Polar RR timestamps lag the true beats by RR_DELAY_S: recovered offset.
    assert abs(summ["median_timestamp_offset_ms"] + RR_DELAY_S * 1000) < 5
    assert summ["mismatched"] == 0
    assert summ["mean_absolute_error_ms"] < 1.5  # 1/1024 s quantisation + interpolation
    assert summ["compared"] >= len(f) - 8  # only beats at edges/gap without an ECG interval


def test_mismatch_detected_when_streams_disagree(tmp_path):
    d, _ = make_session(tmp_path)
    s, g, integ, seg, pk = _run(d)
    f = rr.analyze_rr(s, g, CFG)
    f.loc[60, "rr_ms"] += 80  # corrupt one Polar value (as a parser bug would)
    comp, summ = ecg.compare_rr(f, pk, ecg.spans(s.ecg, seg), CFG)
    assert summ["mismatched"] == 1
    assert comp.loc[60, "status"] == "mismatch" and abs(comp.loc[60, "diff_ms"] - 80) < 2
    assert summ["divergence_periods"][0]["beats"] == 1


def test_flatline_and_extreme_detection(tmp_path):
    d, _ = make_session(tmp_path)
    df = pd.read_csv(d / "ecg.csv")
    df.loc[1000:1300, "ecg_uv"] = 55          # ~2.3 s constant
    df.loc[5000:5002, "ecg_uv"] = 30000       # implausible
    df.to_csv(d / "ecg.csv", index=False)
    s, g, integ, seg, pk = _run(d)
    assert len(integ["flatline_regions"]) == 1
    assert integ["flatline_regions"][0]["samples"] == 301
    assert integ["extreme_value_samples"] == 3 and len(integ["extreme_regions"]) == 1
