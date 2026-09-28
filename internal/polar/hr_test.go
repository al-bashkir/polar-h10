package polar

import (
	"encoding/hex"
	"errors"
	"slices"
	"testing"
)

func mustHex(t *testing.T, s string) []byte {
	t.Helper()
	b, err := hex.DecodeString(s)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func TestParseHR(t *testing.T) {
	tests := []struct {
		name    string
		hex     string
		hr      int
		contact bool
		support bool
		rr      []uint16
	}{
		// Typical H10 frame: contact supported+detected, RR present.
		{"h10 two rr", "16" + "3c" + "5003" + "4d03", 60, true, true, []uint16{0x0350, 0x034d}},
		{"no rr", "06" + "48", 72, true, true, nil},
		{"contact lost", "14" + "48" + "4003", 72, false, true, []uint16{0x0340}},
		{"contact unsupported", "00" + "48", 72, false, false, nil},
		{"uint16 hr", "11" + "2c01" + "4003", 300, false, false, []uint16{0x0340}},
		{"energy then rr", "18" + "48" + "0a00" + "4003", 72, false, false, []uint16{0x0340}},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			m, err := ParseHR(mustHex(t, tc.hex))
			if err != nil {
				t.Fatal(err)
			}
			if m.HeartRate != tc.hr {
				t.Errorf("hr = %d, want %d", m.HeartRate, tc.hr)
			}
			if m.ContactSupported != tc.support || m.ContactDetected != tc.contact {
				t.Errorf("contact supported=%v detected=%v, want %v %v",
					m.ContactSupported, m.ContactDetected, tc.support, tc.contact)
			}
			if !slices.Equal(m.RR, tc.rr) {
				t.Errorf("rr = %v, want %v", m.RR, tc.rr)
			}
		})
	}
}

func TestParseHRMalformed(t *testing.T) {
	for _, h := range []string{"", "16", "01" + "48", "08" + "48" + "0a", "10" + "48" + "40"} {
		if _, err := ParseHR(mustHex(t, h)); err == nil {
			t.Errorf("ParseHR(%q): expected error", h)
		}
	}
	_, err := ParseHR(nil)
	if !errors.Is(err, ErrShortPacket) {
		t.Errorf("err = %v, want ErrShortPacket", err)
	}
}

func TestRRConversion(t *testing.T) {
	cases := []struct {
		raw   uint16
		ms    float64
		nanos int64
	}{
		{1024, 1000, 1_000_000_000},
		{852, 832.03125, 832_031_250},
		{1, 0.9765625, 976_563}, // 976562.5 rounds up
		{0, 0, 0},
		{65535, 63999.0234375, 63_999_023_438},
	}
	for _, c := range cases {
		if got := RRMillis(c.raw); got != c.ms {
			t.Errorf("RRMillis(%d) = %v, want %v", c.raw, got, c.ms)
		}
		if got := RRNanos(c.raw); got != c.nanos {
			t.Errorf("RRNanos(%d) = %v, want %v", c.raw, got, c.nanos)
		}
	}
}

func TestRRTimelineChain(t *testing.T) {
	tl := NewRRTimeline()
	// First notification at 2 s with two beats: last beat pinned to receipt.
	out, rebased := tl.Place([]uint16{1024, 512}, 2_000_000_000, nil)
	if !rebased {
		t.Fatal("first placement must anchor")
	}
	want := []int64{1_500_000_000, 2_000_000_000}
	for i, r := range out {
		if r.Elapsed != want[i] {
			t.Errorf("beat %d at %d, want %d", i, r.Elapsed, want[i])
		}
	}
	// Next notification received 300 ms after the beat: chain continues
	// exactly, no rebase.
	out, rebased = tl.Place([]uint16{1024}, 3_300_000_000, out[:0])
	if rebased || out[0].Elapsed != 3_000_000_000 {
		t.Errorf("continued beat at %d rebased=%v, want 3e9 false", out[0].Elapsed, rebased)
	}
	// Received 100 ms earlier than the chain predicts: the first anchor was
	// late, so the chain slews back by at most 20 ms per notification.
	out, _ = tl.Place([]uint16{1024}, 3_900_000_000, out[:0])
	if out[0].Elapsed != 3_980_000_000 {
		t.Errorf("corrected beat at %d, want 3.98e9", out[0].Elapsed)
	}
	// Five seconds of silence: lost intervals, re-anchor to show the gap.
	out, rebased = tl.Place([]uint16{1024}, 10_000_000_000, out[:0])
	if !rebased || out[0].Elapsed != 10_000_000_000 {
		t.Errorf("after gap beat at %d rebased=%v, want 10e9 true", out[0].Elapsed, rebased)
	}
}

func TestRRTimelineMonotonic(t *testing.T) {
	tl := NewRRTimeline()
	out, _ := tl.Place([]uint16{800}, 5_000_000_000, nil)
	prev := out[0].Elapsed
	// Arrives "early" relative to the chain: the correction must not move the
	// new beat before the previous one.
	out, _ = tl.Place([]uint16{100}, 5_000_000_000, nil)
	if out[0].Elapsed <= prev {
		t.Errorf("beat at %d not after previous %d", out[0].Elapsed, prev)
	}
	prev = out[0].Elapsed
	// Chain far ahead of receipt (implausible): re-anchored, still increasing.
	out, rebased := tl.Place([]uint16{3000}, 3_000_000_000, nil)
	if !rebased || out[0].Elapsed <= prev {
		t.Errorf("rebased=%v beat at %d, previous %d", rebased, out[0].Elapsed, prev)
	}
}

// Over hours, a sensor clock that runs fast or slow against the host must not
// make RR timestamps drift away from receive times.
func TestRRTimelineDrift(t *testing.T) {
	for _, ppm := range []float64{-100, 100} {
		tl := NewRRTimeline()
		var hostBeat float64 = 1e9
		var prev int64
		var out []RRInterval
		for i := range 20000 { // ~4.4 h at 800 ms
			raw := uint16(819) // 799.8 ms on the sensor clock
			hostBeat += float64(RRNanos(raw)) * (1 - ppm*1e-6)
			// Notified 50..950 ms after the beat.
			recv := int64(hostBeat) + 50_000_000 + int64(i%10)*100_000_000
			var rebased bool
			out, rebased = tl.Place([]uint16{raw}, recv, out[:0])
			if rebased && i > 0 {
				t.Fatalf("ppm %v: unexpected rebase at beat %d", ppm, i)
			}
			if out[0].Elapsed <= prev {
				t.Fatalf("ppm %v: non-monotonic at beat %d", ppm, i)
			}
			prev = out[0].Elapsed
		}
		if errMs := (float64(prev) - hostBeat) / 1e6; errMs < 0 || errMs > 60 {
			t.Errorf("ppm %v: final beat %0.1f ms from true time, want within 0..60 ms", ppm, errMs)
		}
	}
}
