package recorder

import (
	"math"
	"testing"
	"time"
)

func rrs(ms ...float64) []LiveRR {
	out := make([]LiveRR, len(ms))
	for i, v := range ms {
		out[i] = LiveRR{MS: v}
	}
	return out
}

// repeat builds n intervals cycling through pattern.
func repeat(n int, pattern ...float64) []LiveRR {
	var ms []float64
	for i := range n {
		ms = append(ms, pattern[i%len(pattern)])
	}
	return rrs(ms...)
}

func TestLiveHRVHandComputed(t *testing.T) {
	// 800, 810, 790, 820, 800 repeated: deviations from the mean 804 are
	// -4, 6, -14, 16, -4; successive differences cycle 10, -20, 30, -20.
	w := repeat(50, 800, 810, 790, 820, 800)
	h := ComputeLiveHRV(w, 60)
	if !h.OK || h.Beats != 50 || h.Excluded != 0 {
		t.Fatalf("%+v", h)
	}
	// SDNN: sum of squares 520 per cycle x 10 cycles, /(n-1)
	if want := math.Sqrt(5200.0 / 49); math.Abs(h.SDNN-want) > 1e-9 {
		t.Errorf("SDNN = %v, want %v", h.SDNN, want)
	}
	// RMSSD: 49 differences: 10 cycles of (10,-20,30,-20,0) minus the last 0
	// -> sum of squares 10*1800 = 18000.
	if want := math.Sqrt(18000.0 / 49); math.Abs(h.RMSSD-want) > 1e-9 {
		t.Errorf("RMSSD = %v, want %v", h.RMSSD, want)
	}
}

func TestLiveHRVWindowAndExclusions(t *testing.T) {
	// 100 intervals of 800 ms = 80 s: only the last 60 s (75 beats) count.
	w := repeat(100, 800)
	if h := ComputeLiveHRV(w, 60); h.Beats != 75 || math.Abs(h.SpanS-60) > 1e-9 {
		t.Errorf("window: %+v", h)
	}
	// A short-long pair is skipped and no difference touches it.
	w = repeat(60, 800, 810)
	w[30].MS, w[31].MS = 420, 1200
	h := ComputeLiveHRV(w, 60)
	if h.Excluded != 2 || math.Abs(h.RMSSD-10) > 1e-9 {
		t.Errorf("exclusion: %+v", h)
	}
	// A break (reconnect) prevents a difference across it: of the 59
	// differences only 900->800 (-100 ms) remains non-zero, 29->30 is dropped.
	w = repeat(60, 800)
	w[30] = LiveRR{MS: 900, Break: true}
	if h := ComputeLiveHRV(w, 60); math.Abs(h.RMSSD-math.Sqrt(10000.0/58)) > 1e-9 {
		t.Errorf("break: RMSSD = %v", h.RMSSD)
	}
}

func TestLiveHRVNeedsData(t *testing.T) {
	if h := ComputeLiveHRV(repeat(15, 800, 820), 60); h.OK {
		t.Errorf("15 beats should not be enough: %+v", h)
	}
	if h := ComputeLiveHRV(nil, 60); h.OK || h.Beats != 0 {
		t.Errorf("%+v", h)
	}
}

func TestAppendRing(t *testing.T) {
	var b []int
	for i := range 10 {
		b = appendRing(b, 4, i)
	}
	if len(b) != 4 || b[0] != 6 || b[3] != 9 {
		t.Errorf("ring = %v", b)
	}
}

func TestStatusCarriesLiveData(t *testing.T) {
	start := time.Now()
	r := New(start, nil, 200, nil)
	for i := range 60 {
		rr := uint16(820)
		if i%2 == 1 {
			rr = 840
		}
		r.Packet(hrPacket(start.Add(time.Duration(i)*800*time.Millisecond), 73, rr))
	}
	r.Packet(ecgPacket(start, 1_000_000_000, 73))
	r.Stop()
	s := r.Status()
	if !s.LiveHRV.OK || s.LiveHRV.Beats != 60 {
		t.Errorf("live HRV = %+v", s.LiveHRV)
	}
	// 820 and 840 units alternate: |diff| = 20/1.024 ms every beat.
	if want := 20 / 1.024; math.Abs(s.LiveHRV.RMSSD-want) > 1e-9 {
		t.Errorf("RMSSD = %v, want %v", s.LiveHRV.RMSSD, want)
	}
	if len(s.RecentECG) != 73 || s.RecentECG[0] != -100 {
		t.Errorf("recent ECG = %d samples, first %d", len(s.RecentECG), s.RecentECG[0])
	}
}
