"""HRV metrics against hand-computed values."""

import math

import numpy as np
import pandas as pd
import pytest

from h10_analysis import hrv
from h10_analysis.models import AnalysisConfig

NN = np.array([800.0, 810.0, 790.0, 820.0, 800.0])
D = np.diff(NN)  # [10, -20, 30, -20]


def test_time_domain_hand_computed():
    td = hrv.time_domain(NN, D)
    assert td["mean_rr_ms"] == 804.0
    # deviations -4, 6, -14, 16, -4 -> squares sum 520 -> /4 = 130
    assert td["sdnn_ms"] == pytest.approx(math.sqrt(130))
    # 100 + 400 + 900 + 400 = 1800 -> /4 = 450
    assert td["rmssd_ms"] == pytest.approx(math.sqrt(450))
    assert td["pnn50_percent"] == 0.0 and td["nn50_count"] == 0
    assert td["median_rr_ms"] == 800.0
    assert td["mad_ms"] == 10.0  # |dev from 800|: 0,10,10,20,0 -> median 10
    assert td["cv_percent"] == pytest.approx(100 * math.sqrt(130) / 804)
    assert td["mean_hr_bpm"] == pytest.approx(np.mean(60000 / NN))
    assert (td["min_rr_ms"], td["max_rr_ms"]) == (790.0, 820.0)
    assert td["valid_duration_s"] == pytest.approx(4.02)


def test_pnn50_boundary():
    d = np.array([60.0, -10.0, 51.0, 50.0])  # exactly 50 is not counted
    td = hrv.time_domain(NN, d)
    assert td["nn50_count"] == 2 and td["pnn50_percent"] == 50.0


def test_poincare_hand_computed():
    x, y = NN[:-1], NN[1:]
    pc = hrv.poincare(x, y)
    # y - x = [10,-20,30,-20]: mean 0, sum sq 1800, var(ddof=1)=600 -> SD1 = sqrt(600)/sqrt(2) = sqrt(300)
    assert pc["sd1_ms"] == pytest.approx(math.sqrt(300))
    # y + x = [1610,1600,1610,1620]: dev 0,-10,0,10 -> var 200/3 -> SD2 = sqrt(200/3/2)
    assert pc["sd2_ms"] == pytest.approx(math.sqrt(100 / 3))
    assert pc["sd1_sd2_ratio"] == pytest.approx(math.sqrt(300) / math.sqrt(100 / 3))


def _flags(nn, excluded=None, segment=None):
    n = len(nn)
    exc = np.zeros(n, dtype=bool) if excluded is None else np.asarray(excluded)
    seg = np.zeros(n, dtype=int) if segment is None else np.asarray(segment)
    t = np.cumsum(nn) / 1000
    adj = np.r_[False, ~exc[1:] & ~exc[:-1] & (seg[1:] == seg[:-1])]
    return pd.DataFrame({"t": t, "rr_ms": nn, "excluded": exc, "segment": seg, "adjacent_prev": adj,
                         "reason": np.where(exc, "local_deviation", "")})


def test_session_hrv_sufficiency():
    cfg = AnalysisConfig()
    short = _flags(np.full(30, 800.0))
    out = hrv.session_hrv(short, cfg)
    assert out["status"] == "insufficient_data" and "time_domain" not in out
    many_bad = _flags(np.full(200, 800.0), excluded=np.arange(200) % 3 == 0)
    assert hrv.session_hrv(many_bad, cfg)["status"] == "too_many_artifacts"
    ok = _flags(800 + 20 * np.sin(np.arange(200)))
    assert hrv.session_hrv(ok, cfg)["status"] == "ok"


def test_session_hrv_excludes_pairs_across_exclusions():
    nn = np.array([800.0, 810.0, 1500.0, 790.0, 820.0] * 30)
    exc = nn == 1500.0
    out = hrv.session_hrv(_flags(nn, exc), AnalysisConfig())
    # Valid pairs never touch 1500: (800,810)=+10 and (790,820)=+30 in each of
    # the 30 repetitions, and (820,800)=-20 between the 29 repetition boundaries.
    expected = math.sqrt((30 * 100 + 30 * 900 + 29 * 400) / 89)
    assert out["time_domain"]["rmssd_ms"] == pytest.approx(expected)
    assert out["time_domain"]["successive_difference_count"] == 89
    assert out["time_domain"]["nn_count"] == 120


def test_windowed_hrv():
    cfg = AnalysisConfig(hrv_window_s=60, hrv_step_s=30)
    nn = 800 + 25 * np.sin(np.arange(450) * 0.7)  # 360 s
    exc = np.zeros(450, dtype=bool)
    exc[200:260] = True  # ~48 s excluded -> windows overlapping it rejected
    w = hrv.windowed_hrv(_flags(nn, exc), cfg, None)
    # beats span 0.8 .. ~360 s; windows start every 30 s while start + 60 <= last beat
    assert len(w) == 10
    assert w["window_start_s"].tolist() == pytest.approx([0.8 + 30 * k for k in range(10)], abs=0.01)
    first = w.iloc[0]
    assert first["status"] == "ok"
    m = (np.cumsum(nn) / 1000 >= first["window_start_s"]) & (np.cumsum(nn) / 1000 < first["window_end_s"])
    assert first["valid_rr_count"] == m.sum()
    assert first["mean_hr_bpm"] == pytest.approx(np.mean(60000 / nn[m]))
    assert first["rmssd_ms"] == pytest.approx(np.sqrt(np.mean(np.diff(nn[m]) ** 2)))
    assert set(w["status"]) == {"ok", "insufficient_coverage"}
    bad = w[w["status"] != "ok"]
    assert bad["rmssd_ms"].isna().all()


def test_frequency_domain_detects_hf_oscillation():
    cfg = AnalysisConfig()
    # RR modulated at 0.25 Hz (HF band) with amplitude 40 ms -> HF power ~ 40^2/2 = 800 ms^2
    t, nn = [0.0], []
    while t[-1] < 400:
        v = 900 + 40 * np.sin(2 * np.pi * 0.25 * t[-1])
        nn.append(v)
        t.append(t[-1] + v / 1000)
    nn = np.array(nn)
    f = _flags(nn)
    f["t"] = np.array(t[1:])
    out = hrv.frequency_domain(f, cfg)
    assert out["status"] == "ok"
    assert out["hf_ms2"] == pytest.approx(800, rel=0.15)
    assert out["lf_ms2"] < 0.05 * out["hf_ms2"]
    assert out["hf_peak_hz"] == pytest.approx(0.25, abs=0.01)
    assert out["vlf_ms2"] is not None  # segment >= 300 s


def test_frequency_domain_refuses_short_data():
    out = hrv.frequency_domain(_flags(np.full(200, 800.0)), AnalysisConfig())
    assert out["status"] == "insufficient_data" and "lf_ms2" not in out
