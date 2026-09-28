"""Heart rate stream statistics (the sensor's own HR values)."""

from __future__ import annotations

import numpy as np

from .models import Session

ABRUPT_BPM = 15  # change between consecutive notifications
ABRUPT_MAX_DT_S = 2.5


def heart_rate_stats(s: Session) -> dict:
    hr = s.hr
    v = hr["heart_rate_bpm"].to_numpy(dtype=float)
    v = v[~np.isnan(v)]
    out: dict = {"samples": int(len(v))}
    if len(v) == 0:
        return out
    t = hr["t"].to_numpy()
    out.update({
        "first_s": float(t[0]), "last_s": float(t[-1]), "span_s": float(t[-1] - t[0]),
        "mean_bpm": float(v.mean()), "median_bpm": float(np.median(v)),
        "min_bpm": float(v.min()), "max_bpm": float(v.max()),
        "std_bpm": float(v.std(ddof=1)) if len(v) > 1 else None,
        "percentiles_bpm": {f"p{p}": float(np.percentile(v, p)) for p in (5, 25, 75, 95)},
        "zero_values": int(np.sum(v == 0)),
    })
    # Abrupt changes between consecutive notifications, listed separately and
    # not interpreted until correlated with RR and ECG.
    vv = hr["heart_rate_bpm"].to_numpy(dtype=float)
    dv, dt = np.diff(vv), np.diff(t)
    idx = np.flatnonzero((np.abs(dv) >= ABRUPT_BPM) & (dt <= ABRUPT_MAX_DT_S))
    out["abrupt_changes"] = [
        {"t_s": float(t[i + 1]), "from_bpm": float(vv[i]), "to_bpm": float(vv[i + 1])} for i in idx
    ]
    out["abrupt_change_threshold_bpm"] = ABRUPT_BPM
    return out
