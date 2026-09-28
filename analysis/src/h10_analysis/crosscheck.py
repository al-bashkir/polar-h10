"""Cross-validation of the HR stream against RR-derived heart rate.

For each HR notification at time t, the RR-derived rate is 60000 / mean(rr)
over the retained RR intervals whose beat time lies in (t - window, t].
The sensor computes its HR value by its own averaging, so small differences
are expected; sustained large differences indicate a data-integrity problem
(parser, timing, or dropped data) and are reported as periods.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .gaps import related_events
from .models import AnalysisConfig, Session

WINDOW_S = 5.0


def hr_vs_rr(s: Session, rr_flags: pd.DataFrame, gaps: pd.DataFrame, cfg: AnalysisConfig) -> tuple[pd.DataFrame, dict]:
    hr_t = s.hr["t"].to_numpy()
    hr_v = s.hr["heart_rate_bpm"].to_numpy(dtype=float)
    keep = ~rr_flags["excluded"].to_numpy(dtype=bool)
    bt = rr_flags["t"].to_numpy()[keep]
    bv = rr_flags["rr_ms"].to_numpy(dtype=float)[keep]
    csum = np.r_[0.0, np.cumsum(bv)]
    hi = np.searchsorted(bt, hr_t, side="right")
    lo = np.searchsorted(bt, hr_t - WINDOW_S, side="right")
    n = hi - lo
    with np.errstate(invalid="ignore", divide="ignore"):
        rr_hr = np.where(n >= 2, 60000 * n / (csum[hi] - csum[lo]), np.nan)
    diff = hr_v - rr_hr
    df = pd.DataFrame({"t": hr_t, "hr_bpm": hr_v, "rr_derived_hr_bpm": rr_hr, "diff_bpm": diff})

    ok = ~np.isnan(diff)
    out: dict = {"window_s": WINDOW_S, "compared": int(ok.sum())}
    if not ok.any():
        out["status"] = "not_enough_data"
        return df, out
    d = diff[ok]
    out.update({
        "status": "ok",
        "mean_difference_bpm": float(d.mean()),
        "mean_absolute_difference_bpm": float(np.abs(d).mean()),
        "max_absolute_difference_bpm": float(np.abs(d).max()),
        "within_5_bpm_percent": float(100 * np.mean(np.abs(d) <= 5)),
    })
    # Sustained disagreement periods.
    bad = ok & (np.abs(np.nan_to_num(diff)) > cfg.hr_disagreement_bpm)
    periods = []
    i = 0
    while i < len(bad):
        if not bad[i]:
            i += 1
            continue
        j = i
        while j + 1 < len(bad) and bad[j + 1]:
            j += 1
        if hr_t[j] - hr_t[i] >= cfg.hr_disagreement_min_s:
            excl = rr_flags[(rr_flags["t"] >= hr_t[i] - WINDOW_S) & (rr_flags["t"] <= hr_t[j]) & rr_flags["excluded"]]
            periods.append({"start_s": float(hr_t[i]), "end_s": float(hr_t[j]),
                            "max_abs_diff_bpm": float(np.nanmax(np.abs(diff[i:j + 1]))),
                            "excluded_rr_in_period": int(len(excl)),
                            "related_event": related_events(s, hr_t[i], hr_t[j], cfg)})
        i = j + 1
    out["disagreement_periods"] = periods
    out["disagreement_threshold_bpm"] = cfg.hr_disagreement_bpm
    out["disagreement_min_duration_s"] = cfg.hr_disagreement_min_s
    return df, out
