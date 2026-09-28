"""Markdown report. Descriptive language only: no medical interpretation."""

from __future__ import annotations

import pandas as pd

from .models import Session

DISCLAIMER = ("This report describes recorded data and its technical quality. It is not a medical assessment "
              "and does not describe the health of the person recorded.")


def _f(x, nd=1, unit=""):
    if x is None:
        return "n/a"
    s = f"{x:,.{nd}f}" if isinstance(x, float) else f"{x:,}"
    return f"{s} {unit}".rstrip()


def _dur(sec) -> str:
    if sec is None:
        return "n/a"
    m, s = divmod(float(sec), 60)
    h, m = divmod(int(m), 60)
    return f"{h}h {m}m {s:.1f}s" if h else f"{m}m {s:.1f}s"


def render(s: Session, q: dict, metrics: dict, summary: dict, gaps: pd.DataFrame, rr_flags: pd.DataFrame,
           plot_files: list[str]) -> str:
    m = s.metadata
    L: list[str] = []
    add = L.append
    add(f"# Session report: {m.get('name') or s.name}\n")
    add(f"> {DISCLAIMER}\n")

    # Session
    add("## Session\n")
    dev = m.get("device") or {}
    add(f"- Directory: `{s.name}`")
    add(f"- Session ID: `{m.get('session_id')}`")
    add(f"- Started: {m.get('started_at')} (UTC; recorder time zone {m.get('timezone')})")
    add(f"- Duration: {_dur(m.get('duration_seconds'))}; status `{m.get('status')}`, stop reason `{m.get('stop_reason')}`")
    add(f"- Device: {dev.get('name')} (ID {dev.get('id')}, firmware {dev.get('firmware')}); "
        f"battery {dev.get('battery_start_percent')} → {dev.get('battery_end_percent')} %")
    rec = m.get("recording") or {}
    yn = {True: "yes", False: "no", None: "n/a"}
    add(f"- Streams: HR {yn[rec.get('hr')]}, RR {yn[rec.get('rr')]}, ECG {yn[rec.get('ecg')]} "
        f"({rec.get('ecg_sample_rate_hz')} Hz nominal); recorder {(m.get('application') or {}).get('version')}")
    a = metrics["analysis"]
    add(f"- Analysis: h10-analysis {a['version']}, Python {a['python']}, config digest `{a['config_digest']}`\n")

    # Data integrity
    add("## Data integrity\n")
    v = q["validation"]
    add(f"Validation found {v['error']} error(s), {v['warning']} warning(s) and {v['info']} note(s).\n")
    if q["issues"]:
        add("| severity | check | finding |\n|---|---|---|")
        for i in q["issues"]:
            add(f"| {i['severity']} | `{i['check']}` | {i['message']} |")
        add("")
    else:
        add("Metadata counters match the files; timestamps are parseable, UTC, strictly increasing and consistent "
            "with `started_at + elapsed_ns`; ECG sample indices are contiguous.\n")

    # Recording quality
    add("## Recording quality\n")
    c = q["classification"]
    add(f"**Data quality: `{c['overall']}`** (recording/data quality only).\n")
    if c["reasons"]:
        add("Reasons:\n")
        for r in c["reasons"]:
            add(f"- {r}")
        add("")
    add(f"The recording lasted {_dur(q['recording_duration_seconds'])}.\n")
    e = q.get("ecg")
    if e:
        add("ECG:")
        add(f"- {_f(e['samples'])} samples; nominal rate {rec.get('ecg_sample_rate_hz')} Hz, measured "
            f"{_f(e.get('measured_sample_rate_hz'), 3)} Hz (sensor clock)")
        add(f"- gaps: {e.get('gaps')}; missing samples: {e.get('missing_samples')}; "
            f"stream continuous: {'yes' if e.get('continuous') else 'no'}")
        add(f"- flatline regions: {e.get('flatline_regions')}; extreme-value samples: {e.get('extreme_value_samples')}\n")
    rr = q["rr"]
    add("RR:")
    add(f"- {rr['samples']} intervals; {rr['artifacts']} excluded as possible artifacts "
        f"({_f(rr['artifact_percentage'], 2, '%')}); {rr['flagged_for_review']} flagged for review only")
    un = rr.get("unaccounted_time_s")
    note = (" (negative: the intervals sum to slightly more than the time they span, within the recorder's "
            "RR timestamp adjustment; no time is missing)" if un is not None and un < 0 else "")
    add(f"- gaps: {rr['gaps']}; time spanned but not covered by RR intervals: {_f(un, 2, 's')}{note}")
    if "excluded_reproduced_by_ecg" in rr:
        add(f"- excluded intervals reproduced by ECG-derived RR: {rr['excluded_reproduced_by_ecg']}; "
            f"not reproduced: {rr['excluded_not_reproduced_by_ecg']}")
    add("")
    hr = q["hr"]
    add(f"HR: {hr['samples']} notifications, gaps: {hr['gaps']}, coverage {_f(hr.get('coverage_percent'), 1, '%')}.\n")
    cn = q["connection"]
    if cn["disconnects"] or cn["disconnected_time_s"]:
        add(f"Bluetooth: {cn['disconnects']} disconnect(s), {cn['reconnections']} reconnection(s), "
            f"{_f(cn['disconnected_time_s'], 1, 's')} disconnected.\n")
    else:
        add("No Bluetooth disconnects were recorded.\n")
    add(f"Dropped BLE packets: {cn['dropped_packets']}; undecodable packets: {cn['decode_errors']}.\n")

    # Heart rate
    add("## Heart rate\n")
    h = metrics["heart_rate"]
    if h.get("samples"):
        p = h["percentiles_bpm"]
        add(f"From the sensor's HR values (hr.csv, {h['samples']} notifications):\n")
        add("| mean | median | min | max | SD | P5 | P95 |\n|---|---|---|---|---|---|---|")
        add(f"| {_f(h['mean_bpm'])} | {_f(h['median_bpm'])} | {_f(h['min_bpm'], 0)} | {_f(h['max_bpm'], 0)} | "
            f"{_f(h['std_bpm'])} | {_f(p['p5'])} | {_f(p['p95'])} |\n")
        add("All values in bpm.\n")
        if h["abrupt_changes"]:
            add(f"Changes of at least {h['abrupt_change_threshold_bpm']} bpm between consecutive notifications:\n")
            for ch in h["abrupt_changes"][:20]:
                add(f"- at {ch['t_s']:.1f} s: {ch['from_bpm']:.0f} → {ch['to_bpm']:.0f} bpm")
            add("")
        hrr = q["cross_checks"]["hr_vs_rr_derived_hr"]
        if hrr.get("status") == "ok":
            add(f"Cross-check with RR-derived HR ({hrr['window_s']:.0f} s window): mean difference "
                f"{_f(hrr['mean_difference_bpm'], 2, 'bpm')}, mean absolute difference "
                f"{_f(hrr['mean_absolute_difference_bpm'], 2, 'bpm')}, {_f(hrr['within_5_bpm_percent'], 1, '%')} within 5 bpm; "
                f"{len(hrr['disagreement_periods'])} sustained disagreement period(s).\n")
    else:
        add("No heart rate data.\n")

    # RR intervals
    add("## RR intervals\n")
    rs = metrics["rr"]
    add(f"{rs['intervals']} raw intervals in {rs['segments']} continuous segment(s). Raw values are kept unchanged; "
        "excluded intervals are removed from the cleaned (NN) series without interpolation.\n")
    if rs["reasons"]:
        add("| reason | intervals |\n|---|---|")
        for k, n in rs["reasons"].items():
            add(f"| `{k}` | {n} |")
        add("")
    add("See `artifacts_rr.csv` for every flagged interval and `cleaned_rr.csv` for the NN series.\n")

    # HRV
    add("## HRV\n")
    hs = metrics["hrv"]["session"]
    if hs["status"] == "ok":
        td, pc = hs["time_domain"], hs["poincare"]
        add(f"Whole session, {td['nn_count']} NN intervals ({_f(td['valid_duration_s'], 1, 's')}), "
            f"{td['successive_difference_count']} valid successive differences:\n")
        add("| metric | value |\n|---|---|")
        for k, lab, nd in (("mean_rr_ms", "mean RR (ms)", 1), ("median_rr_ms", "median RR (ms)", 1),
                           ("mean_hr_bpm", "mean HR from NN (bpm)", 1), ("sdnn_ms", "SDNN (ms)", 1),
                           ("rmssd_ms", "RMSSD (ms)", 1), ("pnn50_percent", "pNN50 (%)", 1),
                           ("mad_ms", "MAD (ms)", 1), ("cv_percent", "CV (%)", 2),
                           ("min_rr_ms", "min RR (ms)", 1), ("max_rr_ms", "max RR (ms)", 1)):
            add(f"| {lab} | {_f(td.get(k), nd)} |")
        add(f"| Poincaré SD1 (ms) | {_f(pc.get('sd1_ms'))} |")
        add(f"| Poincaré SD2 (ms) | {_f(pc.get('sd2_ms'))} |")
        add(f"| SD1/SD2 | {_f(pc.get('sd1_sd2_ratio'), 3)} |\n")
        add("SDNN depends on recording length; compare it only between recordings of similar duration.\n")
    else:
        add(f"Whole-session HRV was not calculated: `{hs['status']}` — {hs['reason']}.\n")
    w = metrics["hrv"]["windowed"]
    wtext = (f"Windowed HRV ({w['window_s']:.0f} s window, {w['step_s']:.0f} s step): {w['windows']} window(s), "
             f"{w['valid_windows']} valid" + (f", rejected: {w['rejected']}" if w["rejected"] else "") + ".")
    if w["valid_windows"]:
        wtext += (f" RMSSD ranged {_f(w['rmssd_ms_range'][0])}–{_f(w['rmssd_ms_range'][1])} ms and mean HR "
                  f"{_f(w['mean_hr_bpm_range'][0])}–{_f(w['mean_hr_bpm_range'][1])} bpm across valid windows "
                  "(`windowed_hrv.csv`, `plots/hrv-windowed.png`).")
    elif w["windows"] == 0:
        wtext += " The RR data is shorter than one window."
    add(wtext + "\n")
    fd = metrics["hrv"]["frequency_domain"]
    if fd["status"] == "ok":
        add(f"Frequency domain (segment {_f(fd['segment']['duration_s'], 0, 's')}): LF {_f(fd['lf_ms2'])} ms², "
            f"HF {_f(fd['hf_ms2'])} ms², LF/HF {_f(fd['lf_hf_ratio'], 2)}"
            + (f", VLF {_f(fd['vlf_ms2'])} ms²" if fd.get("vlf_ms2") is not None else "")
            + ". LF/HF is reported as a signal metric only. Method: see `metrics.json` → hrv.frequency_domain.method.\n")
    else:
        reason = f" — {fd.get('reason')}" if fd.get("reason") else ""
        add(f"Frequency-domain HRV not calculated: `{fd['status']}`{reason}.\n")

    # ECG
    add("## ECG\n")
    er = metrics.get("ecg")
    if er:
        it = er["integrity"]
        sp = it.get("spacing_ms", {})
        add(f"Sample spacing: median {_f(sp.get('median'), 4, 'ms')} (nominal {_f(it.get('expected_period_ms'), 4, 'ms')}), "
            f"min {_f(sp.get('min'), 4)} / max {_f(sp.get('max'), 4)} ms. Measured rate deviates "
            f"{_f(it.get('rate_deviation_percent'), 3, '%')} from nominal.\n")
        amp = it.get("amplitude_uv", {})
        add(f"Amplitude: P1 {_f(amp.get('p1'), 0)} µV, median {_f(amp.get('median'), 0)} µV, P99 {_f(amp.get('p99'), 0)} µV.\n")
        f = er["filter"]
        add(f"A filtered copy ({f['type']}, {f['highpass_hz']}–{f['lowpass_hz']:.1f} Hz, order {f['order']}) is used only "
            "for R-peak detection and plots labelled 'filtered'. The raw ECG is unchanged.\n")
        cmp_ = er.get("rr_comparison")
        if cmp_ and cmp_.get("status") == "ok":
            add(f"R-peaks detected: {er['rpeaks']['count']}. ECG-derived RR vs Polar RR: {cmp_['compared']} beats compared, "
                f"{cmp_['mismatched']} differ by more than {cmp_['mismatch_threshold_ms']:.0f} ms; mean difference "
                f"{_f(cmp_['mean_difference_ms'], 2, 'ms')}, median {_f(cmp_['median_difference_ms'], 2, 'ms')}, mean absolute "
                f"{_f(cmp_['mean_absolute_error_ms'], 2, 'ms')}, max absolute {_f(cmp_['max_absolute_error_ms'], 2, 'ms')}. "
                f"{cmp_['unmatched_polar_beats']} Polar beat(s) had no matching R-peak, "
                f"{cmp_['polar_beats_outside_ecg']} fell outside the ECG recording, and "
                f"{cmp_['ecg_beats_without_polar_beat']} R-peak(s) had no Polar beat.\n")
            add(f"Polar RR timestamps were offset from the ECG R-peaks by a median of "
                f"{_f(cmp_['median_timestamp_offset_ms'], 0, 'ms')} (Polar RR times estimate beat times from notification "
                "arrival; ECG times come from the sensor clock).\n")
    else:
        add("ECG was not analysed (not recorded or disabled).\n")

    # Artifacts
    add("## Artifacts\n")
    exc = rr_flags[rr_flags["excluded"]]
    if len(exc):
        add("Excluded RR intervals (first 30; all in `artifacts_rr.csv`):\n")
        add("| time (s) | RR (ms) | local median (ms) | reason |\n|---|---|---|---|")
        for r in exc.head(30).itertuples():
            add(f"| {r.t:.2f} | {r.rr_ms:.1f} | {r.local_median_ms:.1f} | {r.reason} |")
        add("")
    else:
        add("No RR intervals were excluded.\n")
    regions = metrics["review_regions"]
    if regions:
        add("Regions that may warrant manual review of the ECG:\n")
        for i, r in enumerate(regions, 1):
            add(f"{i}. {r['start_s']:.1f}–{r['end_s']:.1f} s: {r['reason']}")
        add("")

    # Connection events
    add("## Connection events\n")
    ev = s.events[s.events["type"].isin(["session_started", "device_connected", "hr_stream_started",
                                         "ecg_stream_started", "connection_lost", "reconnect_attempt",
                                         "reconnect_failed", "device_reconnected", "data_stalled", "packets_dropped",
                                         "decode_error", "ecg_gap", "ecg_stream_stopped", "session_stopped"])]
    add("| time (UTC) | t (s) | event | details |\n|---|---|---|---|")
    for r in ev.head(60).itertuples():
        ts = r.ts.strftime("%H:%M:%S.%f")[:-3] if not pd.isna(r.ts) else ""
        det = ", ".join(f"{k}={v}" for k, v in r.fields.items() if k not in ("session_id",))
        add(f"| {ts} | {r.t:.3f} | `{r.type}` | {det} |")
    add("")
    if len(gaps):
        add("Detected gaps (`gaps.csv`):\n")
        add("| stream | kind | start (s) | duration (ms) | missing samples | related events |\n|---|---|---|---|---|---|")
        for g in gaps.itertuples():
            add(f"| {g.stream} | {g.kind} | {g.start_elapsed_s:.3f} | {g.duration_ms:.0f} | {g.missing_samples} | "
                f"{g.related_event or '-'} |")
        add("")
    else:
        add("No gaps were detected in the HR, RR or ECG streams.\n")

    # Notable observations
    add("## Notable observations\n")
    obs = _observations(q, metrics, rr_flags)
    for o in obs or ["No notable observations beyond the figures above."]:
        add(f"- {o}")
    add("")

    # Limitations
    add("## Limitations\n")
    add("- Artifact rules are conservative heuristics (see `metrics.json` → analysis.config); they can exclude "
        "genuine beats and miss some artifacts. Excluded intervals include premature-beat-like patterns by NN convention.")
    add("- RR timestamps are estimates reconstructed by the recorder; use `rr_ms` values for HRV and the ECG for "
        "precise beat timing.")
    add("- The R-peak detector is a simple energy-based detector intended for data-integrity checks, not a validated "
        "clinical detector.")
    add("- Short recordings limit HRV: SDNN and frequency-domain metrics depend strongly on duration.")
    add(f"- {DISCLAIMER}\n")

    if plot_files:
        add("## Plots\n")
        for p in plot_files:
            add(f"- [`plots/{p}`](plots/{p})")
        add("")
    return "\n".join(L)


def _observations(q: dict, metrics: dict, rr_flags: pd.DataFrame) -> list[str]:
    obs = []
    reasons = metrics["rr"]["reasons"]
    for key, text in (("short_long_pair", "short-long RR interval pairs"),
                      ("possible_missed_beat", "intervals of about twice the local median"),
                      ("possible_extra_beat", "pairs of short intervals summing to about one local median"),
                      ("out_of_range", "intervals outside the plausible range")):
        if reasons.get(key):
            hit = rr_flags[rr_flags["reason"].str.contains(key)]
            has_ecg_t = "t_ecg" in hit and hit["t_ecg"].notna().all()
            times = hit["t_ecg"] if has_ecg_t else hit["t"]
            where = ", ".join(f"{t:.1f}" for t in times.head(6))
            axis = "ECG time axis" if has_ecg_t else "Polar RR time axis"
            obs.append(f"Atypical RR interval pattern: {reasons[key]} {text} ({axis}, first at {where} s).")
    cmp_ = (metrics.get("ecg") or {}).get("rr_comparison") or {}
    if cmp_.get("compared") and "ecg_check" in rr_flags:
        excl = rr_flags[rr_flags["excluded"]]
        if len(excl):
            same = int((excl["ecg_check"] == "match").sum())
            diff = int(excl["ecg_check"].isin(["mismatch", "unmatched"]).sum())
            if same:
                obs.append(f"{same} of {len(excl)} excluded RR interval(s) are reproduced by the ECG-derived RR "
                           f"intervals within {cmp_['mismatch_threshold_ms']:.0f} ms, so these intervals are present "
                           "in the recorded ECG rather than caused by transmission or parsing. The corresponding "
                           "ECG segments may warrant manual review.")
            if diff:
                obs.append(f"{diff} excluded RR interval(s) are not reproduced by the ECG-derived RR intervals; "
                           "they may be sensor beat-detection or acquisition artifacts.")
        if cmp_.get("mismatched", 0) == 0:
            obs.append(f"ECG-derived RR and Polar RR agree within {cmp_['mismatch_threshold_ms']:.0f} ms for all "
                       f"{cmp_['compared']} compared beats (mean absolute difference {cmp_['mean_absolute_error_ms']:.2f} ms).")
    for p in q["cross_checks"]["hr_vs_rr_derived_hr"].get("disagreement_periods", []):
        obs.append(f"Sensor HR and RR-derived HR differ by up to {p['max_abs_diff_bpm']:.0f} bpm between "
                   f"{p['start_s']:.1f} and {p['end_s']:.1f} s ({p['excluded_rr_in_period']} excluded RR interval(s) nearby"
                   + (f"; events: {p['related_event']}" if p["related_event"] else "") + ").")
    return obs
