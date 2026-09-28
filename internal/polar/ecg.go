package polar

import (
	"encoding/binary"
	"fmt"
	"math"
)

// ECG acquisition settings supported by the Polar H10.
const (
	ECGSampleRate = 130 // Hz
	ECGResolution = 14  // bits
)

// ECGFrame is a decoded PMD data notification carrying ECG samples.
type ECGFrame struct {
	// DeviceTimestamp is the sensor clock time of the LAST sample in the
	// frame, in nanoseconds. The epoch is device-defined (nominally
	// 2000-01-01T00:00:00Z but only valid if the device clock was set), so
	// only differences between timestamps are used.
	DeviceTimestamp uint64
	FrameType       byte
	Samples         []int32 // microvolts, oldest first
}

const pmdHeaderLen = 10 // meas type (1) + timestamp (8) + frame type (1)

// ParseECGFrame decodes a PMD data frame of measurement type ECG:
//
//	[0]    measurement type (0x00 = ECG)
//	[1:9]  uint64 LE timestamp of the last sample, ns
//	[9]    frame type (0x00 = uncompressed 24-bit samples; bit 7 = compressed)
//	[10:]  samples, 3 bytes each, signed 24-bit little-endian, microvolts
func ParseECGFrame(b []byte) (ECGFrame, error) {
	var f ECGFrame
	if len(b) < pmdHeaderLen {
		return f, fmt.Errorf("ecg frame: %w (%d bytes)", ErrShortPacket, len(b))
	}
	if b[0] != MeasECG {
		return f, fmt.Errorf("ecg frame: measurement type %s, want ecg", MeasurementName(b[0]))
	}
	f.DeviceTimestamp = binary.LittleEndian.Uint64(b[1:9])
	f.FrameType = b[9]
	if f.FrameType != 0x00 {
		return f, fmt.Errorf("ecg frame: unsupported frame type 0x%02x", f.FrameType)
	}
	payload := b[pmdHeaderLen:]
	if len(payload) == 0 {
		return f, fmt.Errorf("ecg frame: no samples")
	}
	if len(payload)%3 != 0 {
		return f, fmt.Errorf("ecg frame: payload length %d is not a multiple of 3", len(payload))
	}
	f.Samples = make([]int32, len(payload)/3)
	for i := range f.Samples {
		f.Samples[i] = Int24LE(payload[i*3:])
	}
	return f, nil
}

// Int24LE decodes a signed (two's complement) 24-bit little-endian integer.
func Int24LE(b []byte) int32 {
	v := int32(b[0]) | int32(b[1])<<8 | int32(b[2])<<16
	return v << 8 >> 8 // sign-extend bit 23
}

// ECGSample is one reconstructed ECG sample.
type ECGSample struct {
	Index      int64  // 0-based ordinal of the sample in the recording
	Elapsed    int64  // ns since session start on the host timeline
	DeviceTime uint64 // reconstructed sensor clock time, ns
	Microvolts int32
}

// Placement describes how a frame was placed on the timeline.
type Placement struct {
	// Rebased is non-empty when the device->host clock mapping was
	// (re)established at this frame; it names the reason.
	Rebased string
	// MissingSamples estimates samples lost between the previous frame and
	// this one, derived from device timestamps. 0 when contiguous.
	MissingSamples int64
	// PeriodNs is the sample period used for this frame.
	PeriodNs int64
	// LatencyNs is host receive time minus the mapped time of the last
	// sample: transport latency above the recent minimum.
	LatencyNs int64
}

// ECG clock mapping parameters (see clockMap). A frame arrives about every
// 0.56 s; the window spans ~36 s. The slew limit (0.5 ms per frame, well
// below the 7.7 ms sample period) keeps sample times monotonic and allows
// following sensor/host drift of up to ~900 ppm.
const (
	ecgClockWindow  = 64
	ecgMaxSlewNs    = 500_000
	ecgMinLatencyNs = -1_000_000_000
	ecgMaxLatencyNs = 30_000_000_000
)

// ECGTimeline reconstructs per-sample times from ECG frames.
//
// Sample times come from the sensor clock: within a frame, sample k of n is
// placed at lastTs - (n-1-k)*period, where the period is derived from
// consecutive frame timestamps when they are contiguous (actual sensor rate)
// and is the nominal 1/sampleRate otherwise. Sensor times are mapped to the
// host timeline by a clockMap, so gaps (lost frames, reconnects) remain
// visible and there is no long-term drift against host time.
type ECGTimeline struct {
	nominal int64
	clock   clockMap

	started   bool
	base      uint64 // sensor time origin: sensor(dev) = dev - base
	prevLast  uint64
	nextIndex int64
}

// NewECGTimeline creates a timeline for the given nominal sample rate.
func NewECGTimeline(sampleRateHz int) *ECGTimeline {
	return &ECGTimeline{
		nominal: int64(math.Round(1e9 / float64(sampleRateHz))),
		clock:   newClockMap(ecgClockWindow, ecgMaxSlewNs, ecgMinLatencyNs, ecgMaxLatencyNs),
	}
}

// Place assigns index and time to each sample of f, received on the host at
// recvElapsed (ns since session start), appending them to out.
func (t *ECGTimeline) Place(f ECGFrame, recvElapsed int64, out []ECGSample) ([]ECGSample, Placement) {
	n := int64(len(f.Samples))
	p := Placement{PeriodNs: t.nominal}

	switch {
	case !t.started:
		p.Rebased = "initial"
	case f.DeviceTimestamp <= t.prevLast:
		p.Rebased = "device_clock_not_increasing"
	default:
		delta := int64(f.DeviceTimestamp - t.prevLast)
		missing := int64(math.Round(float64(delta)/float64(t.nominal))) - n
		derived := delta / n
		switch {
		case missing >= 1:
			p.MissingSamples = missing
		case math.Abs(float64(derived-t.nominal)) <= 0.05*float64(t.nominal):
			p.PeriodNs = derived
		}
	}
	if p.Rebased != "" {
		t.started = true
		t.base = f.DeviceTimestamp
		t.clock.reset(recvElapsed)
	} else if t.clock.observe(recvElapsed - t.sensor(f.DeviceTimestamp)) {
		p.Rebased = "clock_mapping_out_of_range"
		p.MissingSamples = 0
	}
	p.LatencyNs = recvElapsed - t.toHost(f.DeviceTimestamp)

	for k, v := range f.Samples {
		dev := f.DeviceTimestamp - uint64((n-1-int64(k))*p.PeriodNs)
		out = append(out, ECGSample{
			Index:      t.nextIndex,
			Elapsed:    t.toHost(dev),
			DeviceTime: dev,
			Microvolts: v,
		})
		t.nextIndex++
	}
	t.prevLast = f.DeviceTimestamp
	return out, p
}

func (t *ECGTimeline) sensor(dev uint64) int64 { return int64(dev - t.base) }

// toHost converts a device time to host elapsed ns.
func (t *ECGTimeline) toHost(dev uint64) int64 { return t.sensor(dev) + t.clock.offset }
