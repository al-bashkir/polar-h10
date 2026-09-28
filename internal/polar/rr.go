package polar

// RRInterval is one RR interval placed on the host timeline.
type RRInterval struct {
	// Elapsed is the estimated time (ns since session start) of the beat that
	// ends this interval.
	Elapsed int64
	Raw     uint16 // 1/1024 s
}

// RR clock mapping parameters (see clockMap). Notifications arrive about
// once per second, so the window spans ~32 s. A beat is reported up to ~1 s
// after it happened, so the chain may lag receive time by that much
// normally; lagging by more than 3 s means intervals were lost (sensor did
// not report them, packets dropped, connection interrupted) and the chain is
// re-anchored. The slew limit (20 ms per notification) is far below any
// physiological RR interval, so beat times stay strictly increasing.
const (
	rrClockWindow  = 32
	rrMaxSlewNs    = 20_000_000
	rrMinLatencyNs = -1_000_000_000
	rrMaxLatencyNs = 3_000_000_000
)

// RRTimeline assigns times to RR intervals, which the sensor sends without
// timestamps inside ~1 Hz heart rate notifications.
//
// Beat times form a chain on the sensor clock (each beat is the previous beat
// plus the RR interval), mapped to the host timeline by a clockMap that
// treats the last beat of each notification as observed at its receive time.
// Within a continuous chain, consecutive timestamps therefore differ by rr
// (up to the small slew corrections). When intervals are lost the chain is
// re-anchored and the gap shows in the timestamps.
type RRTimeline struct {
	clock    clockMap
	sensor   int64 // sensor time of the last beat
	lastHost int64 // host time of the last placed beat
}

// NewRRTimeline creates an RR timeline.
func NewRRTimeline() *RRTimeline {
	return &RRTimeline{clock: newClockMap(rrClockWindow, rrMaxSlewNs, rrMinLatencyNs, rrMaxLatencyNs)}
}

// Place assigns times to the RR intervals from one notification received at
// recvElapsed. It reports whether the chain was (re)anchored.
func (t *RRTimeline) Place(rr []uint16, recvElapsed int64, out []RRInterval) ([]RRInterval, bool) {
	if len(rr) == 0 {
		return out, false
	}
	first := !t.clock.anchored
	s := t.sensor
	for _, v := range rr {
		t.sensor += RRNanos(v)
	}
	rebased := t.clock.observe(recvElapsed - t.sensor)
	if rebased && !first {
		// Never place a beat at or before one already written.
		if firstHost := s + RRNanos(rr[0]) + t.clock.offset; firstHost <= t.lastHost {
			t.clock.offset += t.lastHost - firstHost + 1
		}
	}
	for _, v := range rr {
		s += RRNanos(v)
		t.lastHost = s + t.clock.offset
		out = append(out, RRInterval{Elapsed: t.lastHost, Raw: v})
	}
	return out, rebased
}
