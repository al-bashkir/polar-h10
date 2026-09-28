"""Recording/data quality summary and classification.

The classification describes the recording and the data only. It says
nothing about the person's health.

Rules (first matching level wins; all triggered reasons are listed):

poor
  - any validation error (e.g. counter mismatch, unparseable or inconsistent
    timestamps, duplicated/missing ECG sample indices)
  - more than 10 % of RR intervals excluded as artifacts
  - more than 5 % of ECG samples missing
  - RR coverage below 80 % (sum of RR intervals / time spanned by RR data)
  - HR coverage below 80 % (HR samples / expected ~1 Hz notifications)

usable_with_caution
  - any validation warning (incl. abnormal session status, recorder-reported
    drops, decode errors, disconnects)
  - more than 2 % of RR intervals excluded
  - any ECG gap or missing ECG sample
  - any HR or RR gap, or a connection loss
  - a sustained HR-vs-RR disagreement period
  - more than 5 % of compared beats where ECG-derived RR and Polar RR differ

good
  - none of the above
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .models import Issue, Session

POOR_RR_ARTIFACT_PCT = 10.0
POOR_ECG_MISSING_PCT = 5.0
POOR_COVERAGE_PCT = 80.0
CAUTION_RR_ARTIFACT_PCT = 2.0
CAUTION_RR_MISMATCH_PCT = 5.0
HR_EXPECTED_HZ = 1.0  # the H10 sends heart rate notifications about once per second

RULES = {
    "poor": [
        "validation errors present",
        f"RR artifact percentage > {POOR_RR_ARTIFACT_PCT} %",
        f"ECG missing samples > {POOR_ECG_MISSING_PCT} %",
        f"RR coverage < {POOR_COVERAGE_PCT} %",
        f"HR coverage < {POOR_COVERAGE_PCT} %",
    ],
    "usable_with_caution": [
        "validation warnings present",
        f"RR artifact percentage > {CAUTION_RR_ARTIFACT_PCT} %",
        "any ECG gap or missing ECG samples",
        "any HR/RR gap or connection loss",
        "sustained HR vs RR-derived HR disagreement",
        f"ECG-derived RR vs Polar RR mismatch > {CAUTION_RR_MISMATCH_PCT} % of compared beats",
    ],
    "good": ["none of the above"],
}


def _gap_stats(gaps: pd.DataFrame, stream: str) -> dict:
    g = gaps[gaps["stream"] == stream]
    return {"gaps": int(len(g)), "gap_time_s": round(float((g["duration_ms"].sum()) / 1000), 3),
            "missing_samples_estimated": int(g["missing_samples"].sum())}


def build_quality(s: Session, issues: list[Issue], gaps: pd.DataFrame, rr_summary: dict, rr_flags: pd.DataFrame,
                  ecg_integrity: dict | None, rr_comparison: dict | None, hr_rr: dict) -> dict:
    stats = s.metadata.get("statistics") or {}
    dur = s.duration_s

    hr = {"samples": int(len(s.hr)), **_gap_stats(gaps, "hr")}
    if dur and len(s.hr) > 1:
        hr["coverage_percent"] = round(100 * len(s.hr) / max(dur * HR_EXPECTED_HZ, 1), 2)

    rr = {"samples": rr_summary["intervals"], "artifacts": rr_summary["excluded"],
          "artifact_percentage": rr_summary["artifact_percentage"], "flagged_for_review": rr_summary["flagged_only"],
          "reasons": rr_summary["reasons"], **_gap_stats(gaps, "rr")}
    if "ecg_check" in rr_flags:
        exc = rr_flags.loc[rr_flags["excluded"], "ecg_check"]
        rr["excluded_reproduced_by_ecg"] = int((exc == "match").sum())
        rr["excluded_not_reproduced_by_ecg"] = int(exc.isin(["mismatch", "unmatched"]).sum())
    if len(rr_flags) > 1:
        span = float(rr_flags["t"].iloc[-1] - rr_flags["t"].iloc[0])
        covered = float(rr_flags["rr_ms"].iloc[1:].sum() / 1000)
        rr["span_s"] = round(span, 3)
        rr["unaccounted_time_s"] = round(span - covered, 3)
        rr["coverage_percent"] = round(100 * min(covered / span, 1.0), 2) if span > 0 else None

    conn = _gap_stats(gaps, "connection")
    connection = {"disconnects": int(stats.get("disconnects", 0)), "reconnections": int(stats.get("reconnections", 0)),
                  "disconnected_time_s": conn["gap_time_s"], "dropped_packets": int(stats.get("dropped_packets", 0)),
                  "decode_errors": int(stats.get("decode_errors", 0))}

    q: dict = {
        "recording_duration_seconds": dur,
        "session_status": s.metadata.get("status"),
        "stop_reason": s.metadata.get("stop_reason"),
        "validation": {sev: sum(i.severity == sev for i in issues) for sev in ("error", "warning", "info")},
        "issues": [i.to_dict() for i in issues],
        "hr": hr, "rr": rr, "connection": connection,
    }
    if ecg_integrity is not None:
        e = ecg_integrity
        q["ecg"] = {k: e.get(k) for k in ("samples", "missing_samples", "missing_percentage", "gaps", "continuous",
                                          "measured_sample_rate_hz", "extreme_value_samples")}
        q["ecg"]["flatline_regions"] = len(e.get("flatline_regions", []))
    q["cross_checks"] = {"hr_vs_rr_derived_hr": hr_rr}
    if rr_comparison is not None:
        q["cross_checks"]["ecg_rr_vs_polar_rr"] = rr_comparison
    q["classification"] = classify(q)
    return q


def classify(q: dict) -> dict:
    poor, caution = [], []
    v = q["validation"]
    if v["error"]:
        poor.append(f"{v['error']} validation error(s)")
    if v["warning"]:
        caution.append(f"{v['warning']} validation warning(s)")

    rr = q["rr"]
    ap = rr.get("artifact_percentage") or 0.0
    msg = f"{ap:.1f} % of RR intervals excluded from the NN series"
    if "excluded_reproduced_by_ecg" in rr and rr["artifacts"]:
        msg += f" ({rr['excluded_reproduced_by_ecg']} of {rr['artifacts']} reproduced by ECG-derived RR)"
    if ap > POOR_RR_ARTIFACT_PCT:
        poor.append(msg)
    elif ap > CAUTION_RR_ARTIFACT_PCT:
        caution.append(msg)
    if rr.get("coverage_percent") is not None and rr["coverage_percent"] < POOR_COVERAGE_PCT:
        poor.append(f"RR coverage {rr['coverage_percent']:.1f} %")
    hr = q["hr"]
    if hr.get("coverage_percent") is not None and hr["coverage_percent"] < POOR_COVERAGE_PCT:
        poor.append(f"HR coverage {hr['coverage_percent']:.1f} %")
    if hr["gaps"] or rr["gaps"]:
        caution.append(f"{hr['gaps']} HR gap(s), {rr['gaps']} RR gap(s)")
    if q["connection"]["disconnects"] or q["connection"]["disconnected_time_s"]:
        caution.append(f"{q['connection']['disconnects']} Bluetooth disconnect(s)")

    e = q.get("ecg")
    if e:
        mp = e.get("missing_percentage") or 0.0
        if mp > POOR_ECG_MISSING_PCT:
            poor.append(f"{mp:.2f} % of ECG samples missing")
        elif e.get("gaps") or e.get("missing_samples"):
            caution.append(f"{e.get('gaps')} ECG gap(s), {e.get('missing_samples')} missing sample(s)")

    hrr = q["cross_checks"]["hr_vs_rr_derived_hr"]
    if hrr.get("disagreement_periods"):
        caution.append(f"{len(hrr['disagreement_periods'])} HR vs RR disagreement period(s)")
    cmp_ = q["cross_checks"].get("ecg_rr_vs_polar_rr")
    if cmp_ and cmp_.get("compared"):
        mm = 100 * cmp_["mismatched"] / cmp_["compared"]
        if mm > CAUTION_RR_MISMATCH_PCT:
            caution.append(f"ECG-derived RR differs from Polar RR for {mm:.1f} % of compared beats")

    overall = "poor" if poor else "usable_with_caution" if caution else "good"
    return {"overall": overall, "reasons": poor + caution, "rules": RULES,
            "scope": "recording and data quality only; not a statement about health"}


def ecg_review_regions(ecg_integrity: dict | None, gaps: pd.DataFrame, rr_flags: pd.DataFrame,
                       rr_comparison: dict | None) -> list[dict]:
    """Time ranges worth manual inspection, with the reason."""
    regions: list[dict] = []
    for r in gaps[gaps["stream"] == "ecg"].itertuples():
        regions.append({"start_s": r.start_elapsed_s, "end_s": r.end_elapsed_s, "reason": f"ECG {r.kind} gap"})
    if ecg_integrity:
        for f in ecg_integrity.get("flatline_regions", []):
            regions.append({"start_s": f["start_s"], "end_s": f["end_s"], "reason": "constant ECG value (flatline)"})
        for x in ecg_integrity.get("extreme_regions", []):
            regions.append({"start_s": x["start_s"], "end_s": x["end_s"], "reason": "extreme ECG amplitude"})
    if rr_comparison:
        for p in rr_comparison.get("divergence_periods", []):
            regions.append({"start_s": p["start_s"], "end_s": p["end_s"],
                            "reason": f"ECG-derived RR and Polar RR disagree ({p['beats']} beat(s), ECG time axis)"})
    # Atypical RR patterns (grouped within 10 s), placed on the ECG time axis
    # when the RR/ECG alignment is known (t_ecg), else on the Polar RR axis.
    atyp = rr_flags[rr_flags["excluded"]]
    grp: list[dict] = []
    for r in atyp.itertuples():
        te = getattr(r, "t_ecg", np.nan)
        t, axis = (float(te), "ECG") if not np.isnan(te) else (float(r.t), "Polar RR")
        if grp and grp[-1]["axis"] == axis and t - grp[-1]["end_s"] <= 10:
            grp[-1]["end_s"] = t
            grp[-1]["n"] += 1
        else:
            grp.append({"start_s": t, "end_s": t, "n": 1, "axis": axis})
    for g in grp:
        regions.append({"start_s": g["start_s"], "end_s": g["end_s"],
                        "reason": f"{g['n']} atypical RR interval(s) ({g['axis']} time axis)"})
    # Merge regions that overlap (within 2 s) into one, keeping all reasons.
    merged: list[dict] = []
    for r in sorted(regions, key=lambda r: (r["start_s"], r["end_s"])):
        if merged and r["start_s"] <= merged[-1]["end_s"] + 2.0:
            m = merged[-1]
            m["end_s"] = max(m["end_s"], r["end_s"])
            if r["reason"] not in m["reason"]:
                m["reason"] += "; " + r["reason"]
        else:
            merged.append(dict(r))
    return merged

