# Polar H10 protocol subset used by h10

This documents only what h10 uses. Sources: the Bluetooth GATT specifications
for the standard services, and Polar's public BLE SDK
(<https://github.com/polarofficial/polar-ble-sdk>, "Polar Measurement Data"
documentation) for the PMD service. Field meanings, byte order and sign
handling were verified against a real H10 (firmware 5.0.0). The test fixture
`internal/polar/testdata/synthetic_frames.jsonl` uses the same frame layout
with made-up values.

All multi-byte integers are **little-endian**.

## Discovery

The H10 advertises the local name `Polar H10 <ID>`, where `<ID>` is the 8-hex
digit device ID printed on the sensor (also its serial number). h10 matches
devices by this name. The platform address is not stable across platforms
(macOS exposes a per-host UUID, Linux a MAC address), so the Polar ID is the
device identity used in file names and metadata.

The H10 only advertises and accepts connections while it detects skin contact
(moistened electrodes) and is not connected to another central.

## Standard GATT services

| service | characteristic | use |
|---|---|---|
| Heart Rate `0x180D` | Heart Rate Measurement `0x2A37` (notify) | HR and RR |
| Battery `0x180F` | Battery Level `0x2A19` (read) | percent, 1 byte |
| Device Information `0x180A` | `0x2A29` manufacturer, `0x2A24` model, `0x2A25` serial, `0x2A27` hardware, `0x2A26` firmware, `0x2A28` software (read) | UTF-8 strings |

### Heart Rate Measurement (0x2A37)

```text
byte 0      flags
              bit 0    HR value format: 0 = uint8, 1 = uint16
              bits 1-2 sensor contact: 0b10 supported/not detected,
                       0b11 supported/detected, 0b0x not supported
              bit 3    energy expended present (uint16, kJ)
              bit 4    RR intervals present
next        heart rate, uint8 or uint16, beats/min
[next 2]    energy expended (if bit 3), skipped
[rest]      RR intervals, uint16 each, unit 1/1024 s, oldest first
```

Example frame `10 48 40 03`: flags `0x10` (uint8 HR, RR present, contact
not reported), HR 72 bpm, one RR interval `0x0340` = 832/1024 s = 812.5 ms.
The H10 sets the contact bits in some notifications and not in others
(flags `0x10` and `0x16` were both observed); h10 records contact changes
only when the bits are present.

A frame with an odd number of RR bytes, or shorter than its flags require, is
counted as a decode error.

## Polar Measurement Data (PMD) service

| UUID | name | properties |
|---|---|---|
| `FB005C80-02E7-F387-1CAD-8ACD2D8DF0C8` | PMD service | |
| `FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8` | PMD control point | read, write, indicate |
| `FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8` | PMD data | notify |

### Features (control point read)

Reading the control point returns `0x0F` followed by a bitmask of supported
measurement types (bit n = type n):

| type | measurement |
|---|---|
| 0 | ECG |
| 1 | PPG |
| 2 | ACC |
| 3 | PPI |
| 5 | gyroscope |
| 6 | magnetometer |

The H10 returns `0F 05 00` (ECG + ACC). h10 refuses to record ECG if bit 0 is
not set.

### Commands (control point write)

h10 enables indications on the control point and notifications on the data
characteristic, then writes (with response):

| command | bytes | meaning |
|---|---|---|
| get settings | `01 <type>` | query available settings |
| start ECG | `02 00  00 01 82 00  01 01 0E 00` | start ECG: setting 0 (sample rate), 1 value, 130 Hz; setting 1 (resolution), 1 value, 14 bit |
| stop | `03 <type>` | stop streaming |

Settings are encoded as `[setting type, count, count × value]`, value width by
type: 0 sample rate (uint16), 1 resolution (uint16), 2 range (uint16),
3 range in milli-units (uint32), 4 channels (uint8), 5 factor (uint32).

### Responses (control point indication)

```text
byte 0   0xF0
byte 1   op code of the command
byte 2   measurement type
byte 3   status
byte 4   more frames follow (0/1)          (optional)
byte 5.. op-specific parameters            (optional)
```

Status codes: 0 success, 1 invalid op code, 2 invalid measurement type,
3 not supported, 4 invalid length, 5 invalid parameter, 6 already in state,
7 invalid resolution, 8 invalid sample rate, 9 invalid range, 10 invalid MTU,
11 invalid number of channels, 12 invalid state, 13 device in charger.

h10 treats 6 ("already in state") as success for start/stop. It waits up to
5 s for a response. Responses observed from an H10:

- get ECG settings: `F0 01 00 00 00 00 01 82 00 01 01 0E 00`
  (130 Hz, 14 bit)
- start ECG: `F0 02 00 00 00 00`

### ECG data frame (PMD data notification)

```text
byte 0      measurement type, 0x00 = ECG
bytes 1-8   uint64 sensor timestamp, ns, of the LAST sample in the frame
byte 9      frame type: 0x00 = uncompressed; bit 7 set = compressed (not used
            by the H10 for ECG, rejected as a decode error)
bytes 10..  samples, 3 bytes each: signed 24-bit two's complement, µV
```

At the default MTU the H10 sends 73 samples per frame (229 bytes), about
every 0.56 s. h10 accepts any whole number of samples.

Sign extension: `v = b0 | b1<<8 | b2<<16`, then extend bit 23
(`int32(v<<8) >> 8`). Examples: `38 ff ff` = -200 µV, `dc 05 00` = 1500 µV.

A frame shorter than 10 bytes, with a non-ECG type, a non-zero frame type, no
samples, or a payload that is not a multiple of 3 bytes is a decode error.

## Timestamp assumptions

1. **The frame timestamp is the time of the last sample in the frame.** This
   follows the Polar SDK. It is consistent with observed frames: consecutive
   frame timestamps differ by `n × ~7.698 ms` for frames of n samples.
2. **The sensor clock epoch is not meaningful.** The SDK describes it as
   nanoseconds since 2000-01-01T00:00:00Z. That only holds if the clock has
   been set, and h10 does not set it. An observed sensor timestamp `0x085250EAD5A540BA`
   decodes to 2019-01-01T04:04:29Z, which looks like a default date plus
   uptime rather than the recording date (2026-09-28). h10 uses only differences
   between sensor timestamps and maps them to host time (below). The value of
   the first mapped frame is recorded in the `ecg_clock_anchored` event.
3. **Samples are evenly spaced within a frame** at the sensor's sample period.
   The actual period is taken from consecutive frame timestamps (measured
   ~7.6978 ms, i.e. ~129.9 Hz, versus 7.6923 ms nominal). The nominal period
   is used for the first frame and after a gap.
4. **Lost frames are visible in the sensor timestamps.** If
   `round((ts - prev_ts) / nominal_period) - n ≥ 1`, that many samples are
   missing.
5. **Mapping to host time.** Each frame gives an observation
   `offset = host_receive_time - sensor_time`. The true offset is the smallest
   of these (transport latency ≥ 0), so h10 keeps the minimum observation over
   the last 64 frames (~36 s) and slews its offset towards it by at most
   0.5 ms per frame. This removes the latency of the first frame (tens to
   hundreds of ms). It also follows drift between the sensor and host clocks:
   ~60 ppm measured, ~0.2 s/hour if uncorrected. Times stay strictly
   increasing because 0.5 ms is much smaller than the sample period. Mapped
   times end up late by the minimum BLE transport latency, which the host
   cannot observe.
6. **Reconnects do not reset the sensor clock**, so the mapping continues
   across a reconnect and the gap appears with its true length. If the sensor
   clock goes backwards (sensor reset) or the mapped times diverge from
   receive times by more than -1 s/+30 s, the mapping is re-anchored.
7. **RR intervals use the same approach.** The sensor clock is the cumulative
   RR sum, the observation is the notification receive time for its last beat,
   the window is 32 notifications, the slew limit is 20 ms per notification,
   and the chain is re-anchored if it lags receive time by more than 3 s
   (intervals lost).

## Not used

Accelerometer streaming, PPI, offline recording, clock setting, and the
H10's internal memory are not used.
