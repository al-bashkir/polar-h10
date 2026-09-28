# Session data format (schema version 1)

Recorded files are an API. Column names, units and meanings described here do
not change without incrementing `schema_version` in `metadata.json`.

## Directory layout

Each recording creates one directory under the data directory (default
`~/h10-data`):

```text
<data-dir>/
└── 2026-09-28T03-32-14Z_ABC12345_e4063e02/
    ├── metadata.json
    ├── hr.csv
    ├── rr.csv
    ├── ecg.csv
    ├── events.jsonl
    └── raw/
        └── ble.jsonl        (absent with --no-raw)
```

The directory name is `<start UTC, YYYY-MM-DDTHH-MM-SSZ>_<device id>_<short id>`,
where the short id is the last 8 hex digits (random bits) of the session UUID.
Names sort chronologically. The directory is created exclusively, so an existing
directory is never reused.

Files are opened with `O_EXCL` (never overwritten), flushed to the OS every
second, and `fsync`ed and made read-only (`0444`) when the session is finalized.

## Common conventions

- All timestamps are UTC, RFC 3339 with exactly 9 fractional digits:
  `2026-09-28T03:32:14.123456789Z`.
- `elapsed_ns` (int64): nanoseconds since `started_at`, measured with the host's
  monotonic clock. Wall clock changes (NTP steps, DST, manual changes) during a
  recording do not affect it.
- Invariant: `timestamp == started_at + elapsed_ns` for every row and event.
  Use `elapsed_ns` for arithmetic and `timestamp` for display or joins with
  other data.
- `elapsed_ns` can be slightly negative for ECG samples measured just before the
  session start was recorded (not expected in practice, but do not assume ≥ 0).
- CSV: comma separated, header line, `\n` line endings, no quoting (all values
  are numeric or timestamps).
- Missing data is never fabricated or interpolated. Gaps (disconnects, lost
  packets) show up as larger jumps in `elapsed_ns` and are described in
  `events.jsonl`.

## hr.csv

One row per heart rate notification (about 1 Hz).

| column | type | unit | meaning |
|---|---|---|---|
| `timestamp` | string | UTC | host receive time of the notification |
| `elapsed_ns` | int64 | ns | receive time since session start |
| `heart_rate_bpm` | int | beats/min | heart rate computed by the sensor |

The sensor computes the heart rate from recent beats; it is not an
instantaneous value.

## rr.csv

One row per RR interval (interval between consecutive R waves), oldest first.

| column | type | unit | meaning |
|---|---|---|---|
| `timestamp` | string | UTC | estimated time of the beat that **ends** the interval |
| `elapsed_ns` | int64 | ns | same, since session start |
| `rr_ms` | float | ms | RR interval |

`rr_ms` is exact: the sensor reports intervals in units of 1/1024 s, and
`rr_ms = raw * 1000 / 1024` is written with the shortest exact decimal
representation (for example `832.03125`). No rounding is applied.

**How RR timestamps are derived.** The sensor sends RR intervals without
timestamps, batched in ~1 Hz notifications, up to about a second after the
beat. h10 builds a beat chain on the sensor clock (each beat = previous beat +
RR) and maps it to host time so that the last beat of each notification is at
or before the notification's receive time, following the minimum observed
latency over ~32 s and slewing at most 20 ms per notification (see
`docs/polar-protocol.md`). Consequences:

- Within a continuous chain, `diff(elapsed_ns) ≈ rr_ms` (exact except for slew
  corrections, which are largest during the first ~minute).
- Timestamps are strictly increasing.
- If intervals are lost (chain lags receive time by more than 3 s, e.g. after a
  disconnect), the chain is re-anchored: the gap appears in the timestamps and
  an `rr_chain_rebased` event is written.
- Absolute timestamps are approximate and **late**: the sensor reports RR
  intervals with a delay that the host cannot observe. Compared with R-peaks in
  `ecg.csv`, RR timestamps were measured 1.5–1.9 s late (H10 firmware 5.0.0).
  For HRV metrics use the `rr_ms` sequence; use timestamps to locate gaps. To
  align RR with ECG, match the sequences by interval values (see
  `analysis/README.md`), not by nearest timestamp.

Example (pandas):

```python
rr = pd.read_csv("rr.csv", parse_dates=["timestamp"])
rmssd = (rr.rr_ms.diff() ** 2).mean() ** 0.5
sdnn = rr.rr_ms.std()
pnn50 = (rr.rr_ms.diff().abs() > 50).mean() * 100
```

Check `events.jsonl` for `rr_chain_rebased`, `connection_lost` and
`sensor_contact_changed` before computing metrics across gaps.

## ecg.csv

One row per ECG sample.

| column | type | unit | meaning |
|---|---|---|---|
| `timestamp` | string | UTC | reconstructed measurement time of the sample |
| `elapsed_ns` | int64 | ns | same, since session start |
| `sample_index` | int64 | – | 0-based ordinal of the sample in this file |
| `ecg_uv` | int | µV | ECG voltage (single lead, chest strap electrodes) |

- Nominal rate: `recording.ecg_sample_rate_hz` (130 Hz); resolution
  `recording.ecg_resolution_bits` (14 bit).
- `sample_index` increases by exactly 1 per row, including across gaps. It
  counts recorded samples; it is **not** a position on an ideal sample grid.
  Use `elapsed_ns` to detect gaps.
- The actual sensor sample rate differs slightly from nominal (measured
  ~129.9 Hz on one H10). Timestamps follow the sensor's own clock, so they
  reflect the actual rate.

**How ECG timestamps are derived.** Each BLE frame carries ~73 samples and a
sensor timestamp of the last sample. For each frame:

1. Sample k of n is placed at `last_ts - (n-1-k) * period`, on the sensor clock.
   `period` is `(last_ts - previous_last_ts) / n` when the frame follows the
   previous one without loss (i.e. the actual sensor period), otherwise the
   nominal `1e9 / 130` ns.
2. Sensor time is mapped to host time with an offset that tracks the minimum
   transport latency over the last ~36 s and slews at most 0.5 ms per frame.
   This removes the latency of the first frame and compensates oscillator
   drift between sensor and host (measured ~60 ppm), while keeping timestamps
   strictly increasing.

Consequences:

- Sample spacing follows the sensor clock, except at frame boundaries where a
  slew correction of at most 0.5 ms can apply (mostly during the first minute
  or two, while the initial latency is removed; later corrections are tens of
  µs). For analysis that needs a perfectly uniform grid within a continuous
  segment, use `sample_index / fs` relative to the segment start.
- Absolute timestamps are late by about the minimum BLE transport latency
  (typically 10–40 ms). Host receive time is **not** used as measurement time.
- Lost frames are detected from the sensor timestamps: an `ecg_gap` event
  records the estimated number of missing samples, and `elapsed_ns` jumps
  accordingly. Nothing is interpolated.
- If the mapping becomes implausible (sensor clock goes backwards, e.g. sensor
  reboot, or mapped times diverge from receive times by more than -1 s/+30 s)
  it is re-anchored and an `ecg_clock_rebased` event is written.

Example (pandas):

```python
ecg = pd.read_csv("ecg.csv", parse_dates=["timestamp"])
gaps = ecg[ecg.elapsed_ns.diff() > 2 * 1e9 / 130]   # sample gaps
```

## events.jsonl

One JSON object per line, in the order the recorder processed them. Every
event has:

| field | type | meaning |
|---|---|---|
| `timestamp` | string | UTC time of the event (`started_at + elapsed_ns`) |
| `elapsed_ns` | int64 | ns since session start |
| `type` | string | event type (below) |

plus type-specific fields:

| type | fields | meaning |
|---|---|---|
| `session_started` | `name`, `session_id` | recording started |
| `device_connected` | `name`, `id`, `address`, `firmware` | initial connection |
| `hr_stream_started` | – | HR notifications enabled |
| `ecg_stream_started` | `sample_rate_hz`, `resolution_bits` | PMD ECG stream started |
| `ecg_clock_anchored` | `device_timestamp_ns`, `anchor_elapsed_ns` | first ECG frame mapped to host time |
| `ecg_clock_rebased` | `reason`, `device_timestamp_ns`, `anchor_elapsed_ns` | ECG clock mapping reset |
| `ecg_gap` | `missing_samples_estimated`, `gap_ns`, `next_sample_index` | ECG samples lost before `next_sample_index` |
| `rr_chain_rebased` | `reason` | RR chain re-anchored (lost intervals) |
| `sensor_contact_changed` | `contact` (bool) | electrode contact lost/restored (if the sensor reports it) |
| `battery_level` | `percent` | battery reading (start, every 5 min, end) |
| `connection_lost` | `error` | connection dropped or data stalled |
| `data_stalled` | `silence_ms` | connected but no data for 15 s |
| `reconnect_attempt` | `attempt`, `max_attempts` | reconnect attempt started |
| `reconnect_failed` | `attempt`, `error` | reconnect attempt failed |
| `device_reconnected` | `attempt` | reconnected, streams restored |
| `packets_dropped` | `count`, `total`, `reason` | packets lost because the internal queue was full |
| `decode_error` | `characteristic`, `error`, `data` (hex), `count` | malformed packet (first 100 logged) |
| `ecg_stream_stopped` | – | ECG stream stopped at the end |
| `session_stopped` | `reason` | recording ended |

New event types and fields may be added without a schema version change;
readers should ignore unknown ones.

## raw/ble.jsonl

Every BLE frame received from (and command sent to) the sensor, before
decoding. Intended for debugging and re-parsing with improved decoders.

| field | meaning |
|---|---|
| `received_at` | host time the frame was received (or sent), UTC |
| `elapsed_ns` | same, since session start |
| `direction` | `rx` (from sensor) or `tx` (command written by h10) |
| `characteristic` | GATT characteristic UUID |
| `name` | short name: `hr_measurement`, `pmd_control`, `pmd_data` |
| `data` | frame bytes, lowercase hex |

See `docs/polar-protocol.md` for frame layouts.

## metadata.json

Written when recording starts (`status: "recording"`), rewritten atomically
(temp file + fsync + rename) every 30 s with current statistics, and finalized
when recording stops.

| field | meaning |
|---|---|
| `schema_version` | format version of this directory (1) |
| `session_id` | UUIDv7 |
| `name` | `--name` value (may be empty) |
| `status` | `completed`, `failed`, or `recording` (never finalized: process crashed or was killed; data files are still valid up to the last flush) |
| `stop_reason` | `sigint`, `sigterm`, `duration`, `device_lost` (reconnects exhausted), `device_error`, `write_error` |
| `error` | error message when `status` is `failed` |
| `started_at` | session start, UTC (origin of `elapsed_ns`) |
| `ended_at`, `duration_seconds` | session end; `null` until finalized |
| `timezone`, `utc_offset` | host local time zone at start (IANA name if known) |
| `device` | name, Polar ID, platform address, manufacturer, model, serial, hardware/firmware/software revision, PMD features, `battery_start_percent`, `battery_end_percent` |
| `recording` | enabled streams (`hr`, `rr`, `ecg`), `ecg_sample_rate_hz`, `ecg_resolution_bits`, `raw`, `reconnect_attempts`, `requested_duration_seconds` |
| `timing` | short descriptions of how each timestamp is derived |
| `statistics` | counters, below |
| `application` | `name`, `version`, `go_version` |
| `host` | `os`, `arch`, `hostname` |

`statistics`:

| counter | meaning |
|---|---|
| `hr_samples`, `rr_samples`, `ecg_samples` | rows written to the CSV files |
| `ecg_frames` | ECG frames decoded |
| `ble_packets` | frames received from the sensor |
| `decode_errors` | frames that could not be decoded (kept in raw, logged in events) |
| `dropped_packets` | frames lost because the internal queue was full |
| `ecg_gaps`, `ecg_missing_samples_estimated` | gaps detected from sensor timestamps |
| `ecg_clock_rebases`, `rr_chain_rebases` | clock mapping resets |
| `disconnects`, `reconnections` | connection losses and successful reconnects |

A session with all loss counters at zero and `status: "completed"` has no
known data loss.
