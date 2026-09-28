"""Plots. Display-only transformations (binning) never touch the source data."""

from __future__ import annotations

from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

DPI = 110
GAP_COLORS = {"ecg": "tab:red", "rr": "tab:orange", "hr": "tab:purple", "connection": "black"}


def _save(fig, path: Path) -> str:
    fig.tight_layout()
    fig.savefig(path, dpi=DPI, metadata={"Software": None})
    plt.close(fig)
    return path.name


def _time_axis(duration_s: float) -> tuple[float, str]:
    return (60.0, "time since session start (min)") if duration_s > 600 else (1.0, "time since session start (s)")


def _shade_gaps(ax, gaps: pd.DataFrame, scale: float, streams=("hr", "rr", "ecg", "connection")) -> None:
    seen = set()
    for g in gaps[gaps["stream"].isin(streams)].itertuples():
        label = None if g.stream in seen else f"{g.stream} gap"
        seen.add(g.stream)
        ax.axvspan(g.start_elapsed_s / scale, g.end_elapsed_s / scale, color=GAP_COLORS[g.stream], alpha=0.15,
                   label=label, lw=0)


def heart_rate(out: Path, hr: pd.DataFrame, rr_flags: pd.DataFrame, gaps: pd.DataFrame, dur: float) -> str:
    sc, xl = _time_axis(dur)
    fig, ax = plt.subplots(figsize=(12, 4))
    nn = rr_flags[~rr_flags["excluded"]]
    ax.plot(nn["t"] / sc, 60000 / nn["rr_ms"], ".", ms=2, color="tab:gray", alpha=0.6,
            label="instantaneous HR from cleaned RR (60000/rr_ms)")
    ax.plot(hr["t"] / sc, hr["heart_rate_bpm"], "-", color="tab:red", lw=1.2, label="sensor HR (hr.csv)")
    _shade_gaps(ax, gaps, sc)
    ax.set(xlabel=xl, ylabel="heart rate (bpm)", title="Heart rate")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    return _save(fig, out / "heart-rate.png")


def rr_raw(out: Path, rr_flags: pd.DataFrame, gaps: pd.DataFrame, dur: float) -> str:
    sc, xl = _time_axis(dur)
    fig, ax = plt.subplots(figsize=(12, 4))
    ax.plot(rr_flags["t"] / sc, rr_flags["rr_ms"], "-", color="tab:blue", lw=0.6, alpha=0.7, label="raw RR (rr.csv)")
    exc = rr_flags[rr_flags["excluded"]]
    ax.plot(exc["t"] / sc, exc["rr_ms"], "x", color="tab:red", ms=6, label=f"excluded ({len(exc)})")
    flag = rr_flags[~rr_flags["excluded"] & (rr_flags["reason"] != "")]
    if len(flag):
        ax.plot(flag["t"] / sc, flag["rr_ms"], "o", mfc="none", color="tab:orange", ms=6,
                label=f"flagged for review ({len(flag)})")
    _shade_gaps(ax, gaps, sc, ("rr", "connection"))
    ax.set(xlabel=xl + " — Polar RR time axis", ylabel="RR interval (ms)", title="RR intervals (raw, with flags)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    return _save(fig, out / "rr.png")


def rr_cleaned(out: Path, rr_flags: pd.DataFrame, gaps: pd.DataFrame, dur: float) -> str:
    sc, xl = _time_axis(dur)
    fig, ax = plt.subplots(figsize=(12, 4))
    x = rr_flags["t"].to_numpy() / sc
    y = np.where(rr_flags["excluded"], np.nan, rr_flags["rr_ms"].to_numpy(dtype=float))
    # Break the line at excluded intervals and segment boundaries: no bridging.
    brk = np.r_[False, np.diff(rr_flags["segment"].to_numpy()) != 0]
    y = np.where(brk, np.nan, y)
    ax.plot(x, y, "-", color="tab:green", lw=0.8, label="cleaned NN (excluded intervals left as gaps)")
    _shade_gaps(ax, gaps, sc, ("rr", "connection"))
    ax.set(xlabel=xl + " — Polar RR time axis", ylabel="NN interval (ms)", title="Cleaned RR (NN) intervals")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    return _save(fig, out / "rr-cleaned.png")


def histogram(out: Path, rr_flags: pd.DataFrame) -> str:
    fig, ax = plt.subplots(figsize=(7, 4))
    nn = rr_flags.loc[~rr_flags["excluded"], "rr_ms"]
    exc = rr_flags.loc[rr_flags["excluded"], "rr_ms"]
    lo = float(min(nn.min() if len(nn) else 300, exc.min() if len(exc) else 2000))
    hi = float(max(nn.max() if len(nn) else 300, exc.max() if len(exc) else 300))
    bins = np.arange(lo - 7.8125, hi + 15.625, 7.8125)  # 8 raw units of 1/1024 s
    ax.hist(nn, bins=bins, color="tab:green", alpha=0.8, label=f"cleaned NN (n={len(nn)})")
    if len(exc):
        ax.hist(exc, bins=bins, color="tab:red", alpha=0.7, label=f"excluded (n={len(exc)})")
    ax.set(xlabel="RR interval (ms)", ylabel="count", title="RR interval distribution (bin 7.8125 ms)")
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    return _save(fig, out / "rr-histogram.png")


def poincare(out: Path, rr_flags: pd.DataFrame, pc: dict | None) -> str:
    x = rr_flags["rr_ms"].to_numpy(dtype=float)
    ok = rr_flags["adjacent_prev"].to_numpy(dtype=bool)[1:]
    same_seg = np.diff(rr_flags["segment"].to_numpy()) == 0
    exc = rr_flags["excluded"].to_numpy()
    involves_excluded = same_seg & (exc[1:] | exc[:-1])
    fig, ax = plt.subplots(figsize=(6, 6))
    ax.plot(x[:-1][ok], x[1:][ok], ".", ms=4, color="tab:blue", alpha=0.6, label="cleaned pairs")
    if involves_excluded.any():
        ax.plot(x[:-1][involves_excluded], x[1:][involves_excluded], "x", ms=5, color="tab:red", alpha=0.8,
                label="pairs with an excluded interval")
    lim = [np.nanmin(x) - 20, np.nanmax(x) + 20] if len(x) else [300, 2000]
    ax.plot(lim, lim, "--", color="gray", lw=0.8)
    ax.set(xlim=lim, ylim=lim, xlabel="RR(n) (ms)", ylabel="RR(n+1) (ms)", title="Poincaré plot", aspect="equal")
    if pc and pc.get("sd1_ms") is not None:
        ax.text(0.03, 0.97, f"SD1 = {pc['sd1_ms']:.1f} ms\nSD2 = {pc['sd2_ms']:.1f} ms\n"
                f"SD1/SD2 = {pc['sd1_sd2_ratio']:.3f}", transform=ax.transAxes, va="top", fontsize=9,
                bbox={"fc": "white", "alpha": 0.8})
    ax.legend(loc="lower right", fontsize=8)
    ax.grid(alpha=0.3)
    return _save(fig, out / "poincare.png")


def windowed(out: Path, win: pd.DataFrame, dur: float) -> str | None:
    ok = win[win["status"] == "ok"]
    if ok.empty:
        return None
    sc, xl = _time_axis(dur)
    mid = (win["window_start_s"] + win["window_end_s"]) / 2 / sc
    fig, axes = plt.subplots(3, 1, figsize=(12, 7), sharex=True)
    series = (("mean_hr_bpm", "mean HR (bpm)"), ("rmssd_ms", "RMSSD (ms)"), ("sdnn_ms", "SDNN (ms)"))
    for ax, (col, lab) in zip(axes, series, strict=True):
        ax.plot(mid, win[col], "o-", ms=3)  # NaN for rejected windows leaves gaps
        ax.set_ylabel(lab)
        ax.grid(alpha=0.3)
    axes[0].set_title("Windowed HRV (window centre; rejected windows omitted)")
    axes[-1].set_xlabel(xl)
    return _save(fig, out / "hrv-windowed.png")


def ecg_overview(out: Path, ecg: pd.DataFrame, gaps: pd.DataFrame, regions: list[dict]) -> str:
    t = ecg["t"].to_numpy()
    v = ecg["ecg_uv"].to_numpy(dtype=float)
    nb = min(3000, len(t))
    edges = np.linspace(t[0], t[-1], nb + 1)
    b = np.clip(np.searchsorted(edges, t, side="right") - 1, 0, nb - 1)
    vmin = pd.Series(v).groupby(b).min()
    vmax = pd.Series(v).groupby(b).max()
    centers = (edges[:-1] + edges[1:])[vmin.index] / 2
    fig, ax = plt.subplots(figsize=(14, 4))
    ax.fill_between(centers, vmin.to_numpy(), vmax.to_numpy(), step="mid", color="tab:blue", lw=0,
                    label="raw ECG, min-max per display bin")
    _shade_gaps(ax, gaps, 1.0, ("ecg", "connection"))
    for i, r in enumerate(regions):
        ax.axvspan(r["start_s"] - 0.5, r["end_s"] + 0.5, color="gold", alpha=0.25, lw=0,
                   label="review region" if i == 0 else None)
    ax.set(xlabel="time since session start (s)", ylabel="ECG (µV)",
           title=f"ECG overview ({len(t):,} samples; display binning only)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    return _save(fig, out / "ecg-overview.png")


def _ecg_panel(ax, ecg: pd.DataFrame, rpeaks: pd.DataFrame, a: float, b: float, filt: np.ndarray | None = None,
               polar_ecg_t: np.ndarray | None = None) -> None:
    m = (ecg["t"] >= a) & (ecg["t"] <= b)
    ax.plot(ecg.loc[m, "t"], ecg.loc[m, "ecg_uv"], "-", lw=0.7, color="tab:blue", label="raw ECG (ecg_uv)")
    if filt is not None:
        ax.plot(ecg.loc[m, "t"], filt[m.to_numpy()], "-", lw=0.7, color="tab:green", alpha=0.8,
                label="filtered ECG (0.5-40 Hz zero-phase; not raw)")
    pm = (rpeaks["t"] >= a) & (rpeaks["t"] <= b)
    ax.plot(rpeaks.loc[pm, "t"], rpeaks.loc[pm, "ecg_uv"], "v", color="tab:red", ms=5, label="detected R-peak")
    if polar_ecg_t is not None:
        for i, tt in enumerate(polar_ecg_t[(polar_ecg_t >= a) & (polar_ecg_t <= b)]):
            ax.axvline(tt, color="tab:purple", lw=0.8, alpha=0.5, ls=":", label="Polar RR beat (aligned)" if i == 0 else None)
    ax.set(xlim=(a, b), ylabel="ECG (µV)")
    ax.grid(alpha=0.3)


def ecg_segments(out: Path, ecg: pd.DataFrame, rpeaks: pd.DataFrame, start: float, filt: np.ndarray) -> list[str]:
    t_end = float(ecg["t"].iloc[-1])
    lengths = [L for L in (10, 30, 60) if start + L <= t_end + 1e-9] or [max(t_end - start, 1.0)]
    fig, axes = plt.subplots(len(lengths), 1, figsize=(14, 3.2 * len(lengths)), squeeze=False)
    for ax, L in zip(axes[:, 0], lengths, strict=True):
        _ecg_panel(ax, ecg, rpeaks, start, start + L)
        ax.set_title(f"Raw ECG, {L:g} s from {start:.1f} s")
    axes[0, 0].legend(loc="upper right", fontsize=8)
    axes[-1, 0].set_xlabel("time since session start (s)")
    names = [_save(fig, out / "ecg-segment.png")]
    fig, ax = plt.subplots(figsize=(14, 3.5))
    _ecg_panel(ax, ecg, rpeaks, start, start + min(10, lengths[0]), filt=filt)
    ax.set(title="Raw vs filtered ECG (filtered signal is derived, not recorded)", xlabel="time since session start (s)")
    ax.legend(loc="upper right", fontsize=8)
    names.append(_save(fig, out / "ecg-segment-filtered.png"))
    return names


def ecg_review(out: Path, ecg: pd.DataFrame, rpeaks: pd.DataFrame, regions: list[dict],
               polar_ecg_t: np.ndarray | None, excluded_ecg_t: np.ndarray | None, limit: int = 6) -> list[str]:
    """~12 s of raw ECG per region (starting just before long regions) so complexes stay visible."""
    names = []
    for i, r in enumerate(regions[:limit], 1):
        if r["end_s"] - r["start_s"] <= 8:
            c = (r["start_s"] + r["end_s"]) / 2
            a, b = c - 6, c + 6
        else:
            a, b = r["start_s"] - 3, r["start_s"] + 9
        fig, ax = plt.subplots(figsize=(14, 3.5))
        _ecg_panel(ax, ecg, rpeaks, a, b, polar_ecg_t=polar_ecg_t)
        ax.axvspan(max(r["start_s"], a), min(r["end_s"], b), color="gold", alpha=0.25, lw=0)
        if excluded_ecg_t is not None:
            for k, tt in enumerate(excluded_ecg_t[(excluded_ecg_t >= a) & (excluded_ecg_t <= b)]):
                ax.axvline(tt, color="tab:red", lw=1.2, alpha=0.6,
                           label="end of excluded RR interval (aligned)" if k == 0 else None)
        ax.set(title=f"Review region {i}: {r['reason']} ({r['start_s']:.1f}-{r['end_s']:.1f} s; showing "
                     f"{a:.1f}-{b:.1f} s)", xlabel="time since session start (s)")
        ax.legend(loc="upper right", fontsize=8)
        names.append(_save(fig, out / f"ecg-review-{i}.png"))
    return names


def rr_comparison(out: Path, comp: pd.DataFrame) -> str | None:
    ok = comp[comp["status"].isin(["match", "mismatch"])]
    if ok.empty:
        return None
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(13, 4.5))
    a1.plot(ok["ecg_rr_ms"], ok["polar_rr_ms"], ".", ms=4)
    lim = [ok[["ecg_rr_ms", "polar_rr_ms"]].min().min() - 20, ok[["ecg_rr_ms", "polar_rr_ms"]].max().max() + 20]
    a1.plot(lim, lim, "--", color="gray", lw=0.8)
    a1.set(xlim=lim, ylim=lim, xlabel="ECG-derived RR (ms)", ylabel="Polar RR (ms)", title="Polar RR vs ECG-derived RR",
           aspect="equal")
    a2.plot(ok["t"], ok["diff_ms"], ".", ms=4)
    a2.axhline(0, color="gray", lw=0.8)
    a2.set(xlabel="time (s, Polar RR axis)", ylabel="Polar - ECG (ms)", title="Difference over time")
    for ax in (a1, a2):
        ax.grid(alpha=0.3)
    return _save(fig, out / "rr-ecg-comparison.png")
