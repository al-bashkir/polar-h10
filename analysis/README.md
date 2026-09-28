# h10-analysis

Reproducible, data-integrity-first analysis of Polar H10 sessions recorded by
[`h10`](../README.md): validation, gap detection, RR artifact handling, HR/HRV
metrics, ECG integrity, R-peak cross-checks, plots and a Markdown report.

It describes recorded data and its technical quality. **It is not a medical
tool**: it makes no diagnosis and no statement about the health of the person
recorded. Quality labels (`good`, `usable_with_caution`, `poor`) describe the
recording only.

## Setup

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/) (or any virtualenv
with the dependencies from `pyproject.toml`):

```bash
cd analysis
uv sync                # creates .venv with numpy, pandas, scipy, matplotlib, pytest
```

Dependency versions are pinned in `uv.lock`.

## Usage

```bash
uv run python scripts/analyze_session.py ~/h10-data/2026-09-28T15-39-09Z_ABC12345_0794e357
uv run python scripts/analyze_session.py ~/h10-data/*/ --output analysis-output/
uv run python scripts/compare_sessions.py ~/h10-data/*/
```

With the virtualenv activated (`source .venv/bin/activate`), `python scripts/...` works the same, as do the
installed commands `h10-analyze` and `h10-compare`.

Options (defaults give a complete analysis):

| option | meaning |
|---|---|
| `--output DIR` | output root (default `analysis-output/`, relative to the current directory) |
| `--no-ecg` | skip ECG integrity, filtering, R-peaks and RR/ECG comparison |
| `--no-plots` | skip plots |
| `--rr-min MS`, `--rr-max MS` | plausible RR range (default 300–2000 ms) |
| `--hrv-window S`, `--hrv-step S` | windowed HRV (default 300 s / 30 s) |
| `--config FILE` | JSON object overriding any parameter of `AnalysisConfig` (see `metrics.json` → `analysis.config`) |

The input session directory is only ever read. The tool refuses to write
inside it, and only replaces an existing output directory that contains a
previous `summary.json`.

## Output

```text
analysis-output/<session directory name>/
├── summary.json            stable, versioned per-session summary (summary_schema_version 1)
├── quality.json            validation issues, per-stream quality, cross-checks, classification
├── metrics.json            all metrics + analysis config, versions, dependency versions
├── report.md               human-readable report
├── cleaned_rr.csv          NN series (raw values, excluded intervals removed, no interpolation)
├── artifacts_rr.csv        every flagged RR interval with reason and action (excluded / flagged)
├── gaps.csv                gaps per stream, correlated with events
├── windowed_hrv.csv        sliding-window HRV, including rejected windows and why
├── rpeaks.csv              detected R-peaks (sample_index, timestamp, ecg_uv)
├── rr_ecg_comparison.csv   Polar RR vs ECG-derived RR per beat
├── hr_rr_crosscheck.csv    sensor HR vs RR-derived HR per HR sample
└── plots/                  heart-rate, rr, rr-cleaned, rr-histogram, poincare, hrv-windowed,
                            ecg-overview, ecg-segment, ecg-segment-filtered, ecg-review-N, rr-ecg-comparison
```

The output directory is named after the session directory (sortable, and it
contains the device ID and the short session ID). The full `session_id` is in
`summary.json`.

`compare_sessions.py` writes `analysis-output/comparison/{comparison.csv,comparison.md,trends.png}`. It reuses an
existing per-session analysis if it was made by the same analysis version with the same configuration.

## Pipeline

```text
load → validate → detect gaps → ECG integrity → RR artifacts → cleaned NN →
metrics (HR, HRV, windowed, frequency) → cross-checks → quality → plots → report
```

### Validation (`validation.py`)

- **Metadata:** schema version (only 1 is accepted), required fields, status and stop reason,
  `ended_at − started_at` vs `duration_seconds`, ECG sample rate present, and
  recorder-reported drops, decode errors, gaps, disconnects and re-anchors.
- **Counters:** `hr/rr/ecg_samples` in metadata vs rows in the CSV files. For example,
  "metadata says ecg_samples = 79560, file has 79555 rows" is an error. Packet and frame
  counts are also checked against `raw/ble.jsonl` when present.
- **Timestamps:** parseable, explicitly UTC, never going backwards, no duplicates, and
  `timestamp == started_at + elapsed_ns` within 1 µs. `elapsed_ns` is checked separately.
- **ECG `sample_index`:** starts at 0, increments by exactly 1; duplicates and skips are reported.
- **Events:** readable, known types, and counts consistent with metadata counters.

Malformed values are reported with their row numbers, not silently dropped. A
row without `elapsed_ns` can't be placed on the time axis, so it is excluded
from analysis and reported. A missing required column or file is a load error.

### Gaps (`gaps.py`)

| stream | a gap is… |
|---|---|
| ECG | sample spacing > 1.5 × the measured sample period (expected/missing samples from that period) |
| RR | time between beats exceeds the interval itself by more than 250 ms (beats unaccounted for) |
| HR | spacing > 2.5 s (notifications are ~1 Hz) |
| connection | `connection_lost` → `device_reconnected` (or session end) |
| leading/trailing | a stream starts late / ends early by more than 5 s |

Each gap lists the events within ±3 s (`related_event`). Nothing is interpolated.

### RR artifacts (`rr.py`)

Raw RR values are never modified. Rules, in order:

| reason | action | rule |
|---|---|---|
| `duplicate_timestamp` | exclude | same `elapsed_ns` as the previous interval |
| `out_of_range` | exclude | outside `rr_min_ms`–`rr_max_ms` |
| `possible_missed_beat` | exclude | deviates from the local median by more than 25 % and is ≈ 2 × the median (±30 %) |
| `possible_extra_beat` | exclude | two consecutive short intervals summing to ≈ 1 local median (±15 %) |
| `short_long_pair` | exclude | a deviating interval followed by one deviating the other way by more than 12.5 %, together ≈ 2 medians |
| `local_deviation` | exclude | any other deviation > 25 % from the local median |
| `near_gap` | flag only | within 2 s of a gap boundary or connection event |

The local median is a centred rolling median over 11 intervals. It ignores
duplicates and out-of-range values.

Successive differences (RMSSD, pNN50, Poincaré) are only formed between two
retained intervals that were adjacent in the raw series and in the same
continuous segment. A new segment starts at every RR gap, so no difference
spans an exclusion or a gap.

Premature beats produce the same short–long pattern and are excluded from NN
series by convention. The report checks every excluded interval against the
ECG: if the ECG-derived RR reproduces it, the pattern is present in the
recorded signal (not a transmission or parsing error), and the ECG segment is
listed for manual review.

### HR and HRV (`hr.py`, `hrv.py`)

- **HR statistics** come from the sensor values in `hr.csv`: mean, median, min, max,
  SD, and P5/P25/P75/P95. Changes of 15 bpm or more between consecutive notifications are
  listed separately.
- **Time domain** (from NN intervals):
  - mean, median, min and max RR;
  - `sdnn_ms` (ddof = 1) and `rmssd_ms`;
  - `pnn50_percent` (|Δ| > 50 ms, strictly);
  - `mad_ms` (unscaled median absolute deviation) and `cv_percent`;
  - `mean_hr_bpm` = mean of 60000 / NN.
- **Poincaré:** SD1 = std(RRₙ₊₁ − RRₙ)/√2 and SD2 = std(RRₙ₊₁ + RRₙ)/√2, both with ddof = 1.
- **Sufficiency:** whole-session metrics need at least 50 NN intervals, at least 60 s of NN
  data, and no more than 20 % of intervals excluded. Otherwise the status (`insufficient_data`
  or `too_many_artifacts`) and the reason are reported instead of values.
- **Windowed HRV:** a 300 s window stepped by 30 s. A window is computed only if NN intervals
  cover at least 80 % of it and no more than 10 % of its intervals are excluded. Rejected
  windows are kept with their status.
- **Frequency domain** runs only on the longest gap-free segment, and only if it is at least
  300 s long with no more than 5 % of intervals excluded:
  - NN values at retained beat times → cubic spline at 4 Hz → linear detrend;
  - Welch PSD with a Hann window, 256 s segments (or the whole segment if shorter) and 50 % overlap;
  - band powers by trapezoidal integration: VLF 0.0033–0.04 Hz (only for segments ≥ 300 s),
    LF 0.04–0.15 Hz, HF 0.15–0.40 Hz, all in ms²;
  - LF/HF is reported as a band-power ratio only, with no physiological interpretation.

### ECG (`ecg.py`)

- **Integrity:**
  - sample rate taken from metadata; the measured rate is the median sample spacing,
    following the sensor clock (a real H10 measured about 129.9 Hz);
  - spacing statistics, gaps and missing samples;
  - duplicated indices;
  - flatlines (identical values for at least 1 s);
  - samples with |value| > 10 mV.
- **Filtering:** Butterworth band-pass, 0.5–40 Hz, order 2, zero-phase (`sosfiltfilt`), applied
  separately to each continuous segment. There is no notch filter, because 50/60 Hz lies above
  the 40 Hz corner. The filtered signal is used only for R-peak detection and for plots labelled
  "filtered". The raw ECG is never changed.
- **R-peaks** (a simple detector for integrity checks, not a clinical one):
  - find candidates on a Pan–Tompkins-style energy envelope;
  - locate each peak on the filtered signal;
  - refine to sub-sample precision with parabolic interpolation.
- **Polar RR vs ECG-derived RR:** the two beat sequences are aligned by RR values. First a global
  time offset is chosen from a ±5 s grid to minimise the median |ΔRR|, then the offset is tracked
  beat by beat.
  - Reported: mean, median and absolute differences; mismatches (> 20 ms); unmatched beats; and
    divergence periods.
  - Beats outside the ECG recording are reported separately and not counted as disagreement.

### Cross-checks and quality (`crosscheck.py`, `quality.py`)

- **Sensor HR vs RR-derived HR:** RR-derived HR = 60000 / mean of the retained RR in the
  preceding 5 s. Disagreement of more than 10 bpm lasting at least 5 s is reported as a
  period, together with nearby exclusions and events.
- **Quality classification** (data quality only; all triggered reasons are listed):
  - **poor:**
    - any validation error;
    - more than 10 % of RR intervals excluded;
    - more than 5 % of ECG samples missing;
    - RR or HR coverage below 80 %.
  - **usable_with_caution:**
    - any validation warning;
    - more than 2 % of RR intervals excluded;
    - any ECG, HR or RR gap, or a disconnect;
    - an HR-vs-RR disagreement period;
    - more than 5 % RR/ECG mismatches.
  - **good:** none of the above.

## Findings from real recordings (2026-09-28)

- **Polar RR and ECG agree:** in three sessions, the H10's RR intervals and the ECG-derived RR
  agreed within 1.5 ms for every compared beat, with a mean absolute difference of 0.3–0.5 ms.
- **RR timestamps lag the ECG:** the RR timestamps written by `h10` were 1.5–1.9 s later than
  the corresponding R-peaks in the ECG. The sensor reports RR intervals with a delay, and the
  recorder can only anchor them to notification arrival times.
  - Use `rr_ms` values for HRV and the ECG for precise beat timing.
  - Nearest-in-time matching of RR to ECG beats is therefore wrong by whole beats, which is why
    the comparison aligns by RR values.
- **Recurring short–long pairs:** one session contained recurring short–long RR pairs
  (~420 ms followed by ~930 ms). The ECG reproduces them exactly, so they are in the recorded
  signal. They are excluded from the NN series and listed as ECG review regions. Including them
  would raise RMSSD from about 10 ms to about 29 ms.

## Reproducibility

- **Same inputs, same outputs:** a session analysed with the same code and configuration gives
  byte-identical outputs, PNGs included; this is tested.
- **What `metrics.json` records:** the full configuration, its digest, the analysis version,
  the Python version, and the numpy, pandas, scipy and matplotlib versions.
- **No clock-dependent values:** outputs contain no run timestamps.
- **`summary.json` is versioned:** it carries `summary_schema_version`. Incompatible changes
  increment it.

## Tests

```bash
uv run pytest            # 43 tests, synthetic sessions with known ground truth; no hardware or real data
uvx ruff check src tests scripts
```

`tests/synth.py` writes sessions in the exact h10 file format, with a known ECG
waveform, a 129.9 Hz sensor clock, RR timestamps lagging by 1.5 s, and
optionally a disconnect gap and injected RR artifacts. The metric tests use
small datasets whose results are computed by hand in the test.
