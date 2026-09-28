import numpy as np
import pandas as pd

from h10_analysis import gaps as gapmod
from h10_analysis import rr
from h10_analysis.loader import load_session
from h10_analysis.models import AnalysisConfig, Session
from synth import FS, make_session

CFG = AnalysisConfig()


def test_no_gaps_in_clean_session(tmp_path):
    d, _ = make_session(tmp_path)
    assert gapmod.detect_gaps(load_session(d), CFG).empty


def test_gaps_detected_and_correlated(tmp_path):
    d, truth = make_session(tmp_path, gap=(40.0, 45.0))
    g = gapmod.detect_gaps(load_session(d), CFG)
    streams = set(g["stream"])
    assert streams == {"ecg", "rr", "hr", "connection"}

    e = g[g["stream"] == "ecg"].iloc[0]
    # ECG samples strictly inside (40, 45) are missing: 5 s at 129.9 Hz.
    expected_missing = int(round(5.0 * FS))
    assert abs(e["missing_samples"] - expected_missing) <= 1
    assert e["expected_samples"] == e["missing_samples"]
    assert 4990 < e["duration_ms"] < 5010
    assert "connection_lost@" in e["related_event"] and "device_reconnected@" in e["related_event"]

    c = g[g["stream"] == "connection"].iloc[0]
    assert abs(c["start_elapsed_s"] - 40.5) < 1e-6 and abs(c["end_elapsed_s"] - 44.7) < 1e-6

    r = g[g["stream"] == "rr"].iloc[0]
    assert r["missing_samples"] >= 5  # ~6 beats of ~0.8 s were never reported
    h = g[g["stream"] == "hr"].iloc[0]
    assert h["missing_samples"] == 4  # notifications at 41..44 s missing


def _session_with_rr(values, times=None) -> Session:
    values = np.asarray(values, dtype=float)
    t = np.cumsum(values) / 1000 if times is None else np.asarray(times)
    e = np.round(t * 1e9).astype("int64")
    df = pd.DataFrame({"row": np.arange(1, len(values) + 1), "elapsed_ns": e, "rr_ms": values,
                       "ts": pd.Timestamp("2026-01-01", tz="UTC") + pd.to_timedelta(e, unit="ns"), "t": e / 1e9})
    empty = pd.DataFrame(columns=["line", "ts", "elapsed_ns", "type", "fields", "t"])
    return Session(path=None, name="x", metadata={"duration_seconds": float(t[-1] + 1)}, hr=df.iloc[:0], rr=df,
                   ecg=None, events=empty, started_at=pd.Timestamp("2026-01-01", tz="UTC"))


def _flags(values, times=None, cfg=CFG):
    s = _session_with_rr(values, times)
    return rr.analyze_rr(s, gapmod.detect_gaps(s, cfg), cfg)


BASE = [800, 810, 790, 805, 795, 800, 810, 790, 805, 795, 800, 810, 790, 805, 795, 800]


def reasons(f):
    return {i: r for i, r in enumerate(f["reason"]) if r}


def test_clean_rr_has_no_flags():
    f = _flags(BASE)
    assert reasons(f) == {} and not f["excluded"].any()
    assert f["adjacent_prev"].tolist() == [False] + [True] * (len(BASE) - 1)


def test_out_of_range_and_isolated_jump():
    v = list(BASE)
    v[3] = 250          # implausible
    v[8] = 1150         # isolated extreme (ratio ~1.44)
    f = _flags(v)
    assert reasons(f) == {3: "out_of_range", 8: "local_deviation"}
    assert f.loc[[3, 8], "excluded"].all()


def test_missed_beat():
    v = list(BASE)
    v[6] = 1600         # two intervals merged
    assert reasons(_flags(v)) == {6: "possible_missed_beat"}


def test_extra_beat():
    v = BASE[:6] + [300, 500] + BASE[7:]  # one interval split in two
    assert reasons(_flags(v)) == {6: "possible_extra_beat", 7: "possible_extra_beat"}


def test_short_long_pair_with_incomplete_compensation():
    v = BASE[:6] + [420, 950] + BASE[8:]  # like the pattern seen in real data
    assert reasons(_flags(v)) == {6: "short_long_pair", 7: "short_long_pair"}


def test_duplicate_timestamp():
    t = np.cumsum(BASE) / 1000
    t[5] = t[4]
    f = _flags(BASE, t)
    assert "duplicate_timestamp" in f["reason"][5] and f["excluded"][5]


def test_rr_gap_starts_segment_and_flags_neighbours():
    t = np.cumsum(BASE) / 1000
    t[8:] += 5.0  # 5 s of unreported beats before interval 8
    f = _flags(BASE, t)
    assert f["segment"].tolist() == [0] * 8 + [1] * 8
    # near_gap is flag-only: nothing excluded
    assert not f["excluded"].any()
    assert f["reason"][7] == "near_gap" and f["reason"][8] == "near_gap"
    # no successive difference across the gap
    assert not f["adjacent_prev"][8]


def test_successive_differences_skip_exclusions():
    v = [800, 810, 250, 790, 820, 800] + BASE
    f = _flags(v)
    d = rr.successive_differences(f)
    # pairs (0,1), (3,4), (4,5), (5,6)... ; nothing involving index 2
    assert d[0] == 10 and d[1] == 30 and d[2] == -20
    assert len(d) == len(v) - 1 - 2  # two pairs touch the excluded interval


def test_summary_counts():
    v = list(BASE)
    v[3] = 250
    s = rr.summary(_flags(v))
    assert s["intervals"] == 16 and s["excluded"] == 1
    assert s["artifact_percentage"] == 6.25
    assert s["reasons"] == {"out_of_range": 1}
