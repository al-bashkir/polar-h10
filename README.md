# h10

A command line recorder for the **Polar H10** chest strap over Bluetooth Low
Energy. It records heart rate, RR intervals and raw ECG (130 Hz) into plain
CSV/JSONL session directories that load directly into pandas, R or any other
tool.

h10 only acquires and stores data. It does not interpret it medically.

```text
$ h10 scan
DEVICE      ID         RSSI   ADDRESS
Polar H10   ABC12345   -64    3f2a1c9e-5b7d-4e8a-9c61-2d4f8e0a7b13

$ h10 record --name resting-test --duration 10m
```

## Requirements

- Go 1.25+ to build (developed with Go 1.27).
- **macOS**: CoreBluetooth; building needs cgo (Xcode command line tools).
- **Linux**: BlueZ 5 with `bluetoothd` running and access to the system D-Bus.
  Pure Go, no cgo needed.
- A Polar H10, worn with moistened electrodes. The strap sleeps otherwise and
  does not advertise. It must not be connected to another app or phone,
  because the H10 accepts one connection at a time for streaming.

## Build

```bash
make build          # -> bin/h10
./bin/h10 version
make install        # into $GOBIN / $GOPATH/bin
make linux          # -> dist/h10-linux-amd64, dist/h10-linux-arm64 (static, no cgo)
make help           # all targets
```

The version comes from an exact git tag (`v0.2.0` -> `0.2.0`), or set it with
`make VERSION=0.2.0 build`. Without make: `go build -o h10 ./cmd/h10`.

## Bluetooth permissions

- **macOS**: the first run triggers a Bluetooth permission prompt for the app
  that runs `h10` (Terminal, iTerm, VS Code, ...). If it was denied, enable it
  in *System Settings → Privacy & Security → Bluetooth* and restart that app.
  The error is `bluetooth is not authorized for this app`.
- **Linux**: your user must be allowed to use BlueZ over D-Bus. On most
  distributions desktop users are; otherwise add yourself to the `bluetooth`
  group (then log in again). Check with `bluetoothctl show` that an adapter
  exists and is powered.

## Usage

```text
h10 [--verbose | --log-level debug|info|warn|error] [--config FILE] <command> [flags]
```

### scan

```bash
h10 scan               # list Polar H10 devices for 10 s
h10 scan --timeout 20s
h10 scan --all         # all BLE devices (debugging)
```

### info

Connects and shows name, ID, battery, manufacturer/model/serial, hardware,
firmware and software revisions, supported measurements and ECG settings.

```bash
h10 info
h10 info --device ABC12345
```

### monitor

Shows live HR, RR, rolling RMSSD/SDNN, contact, battery, ECG status, packet and
drop counters and a trace of the last 4 s of ECG, in a block that updates in
place. Nothing is written to disk. Ctrl+C stops. The `record` display shows the
same block.

```text
Polar H10 ABC12345
────────────────────────────────
Status      connected
HR          73 bpm
RR          785 ms
RMSSD       15 ms  (last 60 s, 71 beats)
SDNN        24 ms
Contact     ok
Battery     100 %
ECG         streaming
Packets     177
Dropped     0
Duration    00:01:12

ECG (last 4 s)
▂▁█▂▂▂▂▁▁▂▁▇▃▂▂▃▁▁▂▂▁█▂▂▂▂▁▁▂▁▇▃▂▂▃▁▁▁▂▁█▂▂▂▂▁▁▂
```

- **RMSSD and SDNN** use the RR intervals from the last 60 s. They skip intervals outside
  300–2000 ms or more than 25 % from the window median, and never compute a difference
  across a skipped beat or a reconnect.
- **Collecting:** they show `collecting (n/60 s)` until at least 20 clean intervals covering
  30 s are available. These live values are only an indication; use the `analysis/` pipeline
  for reported metrics.
- **ECG trace:** each character is the peak-to-peak range of about 83 ms of signal, so QRS
  complexes stand out as tall bars. It is a display aid, not the waveform.

```bash
h10 monitor
h10 monitor --device ABC12345 --no-ecg
```

### record

```bash
h10 record                              # until Ctrl+C
h10 record --name morning-rest --duration 10m
h10 record --device ABC12345 --output ~/h10-data
h10 record --no-raw                     # skip raw/ble.jsonl
h10 record --no-ecg                     # HR/RR only
h10 record --reconnect-attempts 10
```

Recording stops on Ctrl+C (or SIGTERM), when `--duration` expires, when the
device is lost and reconnect attempts are exhausted, or when writing to disk
fails. On a normal stop h10 stops the ECG stream, reads the final battery
level, flushes and fsyncs all files, and finalizes `metadata.json`. A second
Ctrl+C exits immediately. Data is still flushed every second, but
`metadata.json` then stays in the `recording` state.

If the connection drops, h10 records the event and reconnects: first directly,
then by scanning for the same device ID. It retries up to 5 times with backoff
(2 s, 4 s, 8 s, ... up to 30 s), restarts the streams and continues the
**same** session. The gap stays visible in the timestamps. Nothing is
interpolated.

Exit status is 0 for a completed session and 1 if the session failed (device
lost, write error). The summary shows the sample and loss counters.

### sessions

```bash
h10 sessions
h10 sessions --output ~/h10-data
```

```text
DATE               DURATION   DEVICE     NAME           STATUS      DIRECTORY
2026-09-28 20:39   2m30s      ABC12345   drift-test     completed   2026-09-28T15-39-09Z_ABC12345_0794e357
```

Times in `DATE` are local. `incomplete` means the session was never finalized
(still running, or the process was killed).

## Where sessions are stored

The data directory defaults to `~/h10-data`. Each recording gets its own
immutable directory:

```text
~/h10-data/2026-09-28T15-39-09Z_ABC12345_0794e357/
├── metadata.json     session, device, settings, counters
├── hr.csv            timestamp,elapsed_ns,heart_rate_bpm
├── rr.csv            timestamp,elapsed_ns,rr_ms
├── ecg.csv           timestamp,elapsed_ns,sample_index,ecg_uv
├── events.jsonl      connection, stream, gap, drop and error events
└── raw/ble.jsonl     every BLE frame as hex, for re-parsing and debugging
```

```python
import pandas as pd
ecg = pd.read_csv("ecg.csv", parse_dates=["timestamp"])
rr = pd.read_csv("rr.csv", parse_dates=["timestamp"])
hr = pd.read_csv("hr.csv", parse_dates=["timestamp"])
```

See [docs/data-format.md](docs/data-format.md) for every file and column,
units, and how timestamps are derived. See
[docs/polar-protocol.md](docs/polar-protocol.md) for the protocol details and
assumptions.

## Configuration (optional)

Flags always win. An optional JSON file is read from
`~/Library/Application Support/h10/config.json` (macOS) or
`~/.config/h10/config.json` (Linux), or from `--config FILE`:

```json
{
  "data_dir": "~/h10-data",
  "preferred_device": "ABC12345",
  "raw_recording": true,
  "reconnect_attempts": 5
}
```

## Logging

Normal runs are quiet: only the status display, warnings (dropped packets,
decode errors, ECG gaps) and errors. `--verbose` logs events at info level;
`--log-level debug` adds more. Logs go to stderr and never include
per-sample data.

## Troubleshooting

| symptom | what to check |
|---|---|
| `polar H10 not found` | Wear the strap and moisten the electrodes. Close other apps (Polar Flow, Elite HRV, ...) and disconnect the H10 from your phone. Move closer. Run `h10 scan --all`. |
| `bluetooth is not authorized for this app` (macOS) | Grant Bluetooth permission to your terminal app, then restart it. |
| `AccessDenied` / `ServiceUnknown` (Linux) | Start `bluetoothd` (`systemctl start bluetooth`), check `bluetoothctl show`, check D-Bus permissions or group membership. |
| `bluetooth is powered off` | Turn Bluetooth on. |
| `ecg: not supported by device` | The device is not an H10 (or does not offer PMD ECG). Use `--no-ecg`. |
| `status 13: device in charger` | The sensor refuses streaming while charging/connected to USB. |
| Frequent reconnects, `ecg_gap` events | Weak signal: reduce distance, avoid body blocking the line of sight, check the battery. |
| HR shows `--`, `Contact NO CONTACT` | Electrodes are dry or the strap is loose. |
| `status: failed`, `stop_reason: write_error` | Disk full or not writable; the data up to the failure is on disk. |

## Development

```bash
make test                            # unit tests, no hardware needed
make check                           # go vet + staticcheck (macOS and Linux) + race tests
make test-hardware                   # needs a worn H10 in range
H10_DEVICE=ABC12345 make test-hardware
```

Unit tests use synthetic BLE frames (`internal/polar/testdata`), never
recorded data.

Layout:

```text
cmd/h10/            main
internal/cli/       commands, config, status display
internal/ble/       thin wrapper over tinygo.org/x/bluetooth (CoreBluetooth / BlueZ)
internal/polar/     Polar H10 protocol: HR, PMD, ECG parsing, timestamp reconstruction
internal/recorder/  packet processing, counters, single writer goroutine, reconnect loop
internal/storage/   session directory, CSV/JSONL writers, atomic metadata
```
