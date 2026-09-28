package polar

import (
	"encoding/binary"
	"testing"
)

// ecgFrame builds a PMD ECG data frame for tests.
func ecgFrame(ts uint64, samples ...int32) []byte {
	b := make([]byte, 10, 10+3*len(samples))
	b[0] = MeasECG
	binary.LittleEndian.PutUint64(b[1:], ts)
	for _, s := range samples {
		b = append(b, byte(s), byte(s>>8), byte(s>>16))
	}
	return b
}

func TestInt24LE(t *testing.T) {
	cases := map[[3]byte]int32{
		{0x00, 0x00, 0x00}: 0,
		{0x7c, 0x00, 0x00}: 124,
		{0xff, 0xff, 0xff}: -1,
		{0x00, 0x00, 0x80}: -8388608,
		{0xff, 0xff, 0x7f}: 8388607,
		{0x38, 0xff, 0xff}: -200,
		{0x10, 0x27, 0x00}: 10000,
	}
	for in, want := range cases {
		if got := Int24LE(in[:]); got != want {
			t.Errorf("Int24LE(% x) = %d, want %d", in, got, want)
		}
	}
}

func TestParseECGFrame(t *testing.T) {
	// Static fixture: ECG frame, ts=0x0102030405060708, three samples 124, -1, -200.
	raw := mustHex(t, "00"+"0807060504030201"+"00"+"7c0000"+"ffffff"+"38ffff")
	f, err := ParseECGFrame(raw)
	if err != nil {
		t.Fatal(err)
	}
	if f.DeviceTimestamp != 0x0102030405060708 {
		t.Errorf("timestamp = %#x", f.DeviceTimestamp)
	}
	want := []int32{124, -1, -200}
	if len(f.Samples) != len(want) {
		t.Fatalf("samples = %v", f.Samples)
	}
	for i := range want {
		if f.Samples[i] != want[i] {
			t.Errorf("sample %d = %d, want %d", i, f.Samples[i], want[i])
		}
	}
}

func TestParseECGFrameMalformed(t *testing.T) {
	cases := map[string][]byte{
		"empty":        nil,
		"short header": {0x00, 0x01, 0x02},
		"acc frame":    append([]byte{MeasACC}, make([]byte, 12)...),
		"no samples":   ecgFrame(1),
		"partial":      append(ecgFrame(1, 5), 0x01),
		"compressed":   func() []byte { b := ecgFrame(1, 5); b[9] = 0x80; return b }(),
	}
	for name, b := range cases {
		if _, err := ParseECGFrame(b); err == nil {
			t.Errorf("%s: expected error", name)
		}
	}
}

func TestECGTimelineReconstruction(t *testing.T) {
	const period = 7_692_308 // round(1e9/130)
	tl := NewECGTimeline(130)

	// First frame: 3 samples, last sample at device time 1_000_000_000,
	// received at host elapsed 50 ms.
	f1, _ := ParseECGFrame(ecgFrame(1_000_000_000, 1, 2, 3))
	s, p := tl.Place(f1, 50_000_000, nil)
	if p.Rebased != "initial" || p.PeriodNs != period {
		t.Fatalf("placement = %+v", p)
	}
	wantElapsed := []int64{50_000_000 - 2*period, 50_000_000 - period, 50_000_000}
	for i, smp := range s {
		if smp.Index != int64(i) || smp.Elapsed != wantElapsed[i] || smp.Microvolts != int32(i+1) {
			t.Errorf("sample %d = %+v, want elapsed %d", i, smp, wantElapsed[i])
		}
	}

	// Second frame is contiguous per device clock but arrives late (host
	// jitter): its timestamps must follow the device clock, not arrival.
	// Actual device period here is 7_700_000 ns (slightly slow sensor).
	f2, _ := ParseECGFrame(ecgFrame(1_000_000_000+3*7_700_000, 4, 5, 6))
	s, p = tl.Place(f2, 400_000_000, s[:0])
	if p.Rebased != "" || p.MissingSamples != 0 || p.PeriodNs != 7_700_000 {
		t.Fatalf("placement = %+v", p)
	}
	for i, smp := range s {
		want := 50_000_000 + int64(i+1)*7_700_000
		if smp.Elapsed != want || smp.Index != int64(3+i) {
			t.Errorf("sample %d = %+v, want elapsed %d", i, smp, want)
		}
	}
	if p.LatencyNs != 400_000_000-(50_000_000+3*7_700_000) {
		t.Errorf("latency = %d", p.LatencyNs)
	}

	// Third frame after 10 missing samples: gap detected, nominal period,
	// indices continue without holes, timestamps show the gap.
	last2 := uint64(1_000_000_000 + 3*7_700_000)
	f3, _ := ParseECGFrame(ecgFrame(last2+13*period, 7, 8, 9))
	s, p = tl.Place(f3, 600_000_000, s[:0])
	if p.MissingSamples != 10 || p.Rebased != "" {
		t.Fatalf("placement = %+v", p)
	}
	if s[0].Index != 6 {
		t.Errorf("index = %d, want 6", s[0].Index)
	}
	firstNew := int64(s[0].DeviceTime - last2)
	if firstNew != 11*period {
		t.Errorf("first sample after gap is %d ns after previous, want %d", firstNew, 11*period)
	}
}

func TestECGTimelineRebase(t *testing.T) {
	tl := NewECGTimeline(130)
	f, _ := ParseECGFrame(ecgFrame(5_000_000_000, 1))
	tl.Place(f, 0, nil)

	// Device clock went backwards (sensor reboot): re-anchor at receipt.
	f, _ = ParseECGFrame(ecgFrame(1_000, 1))
	s, p := tl.Place(f, 60_000_000_000, nil)
	if p.Rebased != "device_clock_not_increasing" || s[0].Elapsed != 60_000_000_000 {
		t.Errorf("placement = %+v sample = %+v", p, s[0])
	}

	// Mapped time far in the future of receipt: implausible, re-anchor.
	f, _ = ParseECGFrame(ecgFrame(1_000+100_000_000_000, 1))
	_, p = tl.Place(f, 61_000_000_000, nil)
	if p.Rebased != "clock_mapping_out_of_range" {
		t.Errorf("placement = %+v", p)
	}
}

// Over hours, sensor/host oscillator drift must not accumulate into the
// mapping, and sample times must stay strictly increasing.
func TestECGTimelineDrift(t *testing.T) {
	const n = 73
	for _, ppm := range []float64{-100, 60, 100} {
		tl := NewECGTimeline(130)
		dev := uint64(599630195227309176) // real H10 clock value
		const devPeriod = 7_697_800       // measured H10 period
		trueHost := 2e9
		var prev int64 = -1 << 62
		var buf []ECGSample
		frame := ECGFrame{Samples: make([]int32, n)}
		for i := range 30000 { // ~4.7 h
			dev += n * devPeriod
			trueHost += n * devPeriod * (1 - ppm*1e-6)
			frame.DeviceTimestamp = dev
			// Transport latency 20..80 ms, first frame delayed 150 ms.
			lat := 20e6 + float64(i%7)*10e6
			if i == 0 {
				lat = 150e6
			}
			var p Placement
			buf, p = tl.Place(frame, int64(trueHost+lat), buf[:0])
			if p.Rebased != "" && i > 0 {
				t.Fatalf("ppm %v: rebase %q at frame %d", ppm, p.Rebased, i)
			}
			if buf[0].Elapsed <= prev {
				t.Fatalf("ppm %v: non-monotonic at frame %d", ppm, i)
			}
			prev = buf[n-1].Elapsed
		}
		// The mapping converges to truth + minimum transport latency (20 ms):
		// the host cannot observe one-way latency. Drift must not accumulate.
		if errMs := (float64(prev) - trueHost) / 1e6; errMs < 10 || errMs > 21 {
			t.Errorf("ppm %v: last sample mapped %0.2f ms after truth, want 10..21", ppm, errMs)
		}
	}
}
