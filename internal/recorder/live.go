package recorder

import (
	"math"
	"slices"
)

// Live display buffers. They are only used for the status display and never
// affect what is written to storage.
const (
	liveECGSamples  = 650 // ~5 s at 130 Hz
	liveRRIntervals = 300 // > 60 s even at 250 bpm
)

// LiveRR is one RR interval in the live buffer. Break marks an interval that
// must not form a successive difference with the one before it (after an
// RR chain re-anchor or a reconnect).
type LiveRR struct {
	MS    float64
	Break bool
}

// LiveHRV is a rolling HRV estimate for display.
type LiveHRV struct {
	OK       bool    // enough clean data in the window
	RMSSD    float64 // ms
	SDNN     float64 // ms
	Beats    int     // retained intervals in the window
	Excluded int     // intervals skipped as implausible
	SpanS    float64 // seconds of RR data in the window
}

// Minimum data for a live estimate.
const (
	liveMinBeats = 20
	liveMinSpanS = 30.0
	liveMinDiffs = 10
)

// ComputeLiveHRV computes RMSSD and SDNN over the most recent intervals
// spanning at most windowS seconds. It applies the same conservative rules as
// the offline analysis: intervals outside 300-2000 ms or deviating more than
// 25 % from the window median are skipped, and successive differences are
// only formed between two retained, adjacent intervals with no break between.
func ComputeLiveHRV(rr []LiveRR, windowS float64) LiveHRV {
	start, sum := len(rr), 0.0
	for start > 0 && sum+rr[start-1].MS <= windowS*1000 {
		start--
		sum += rr[start].MS
	}
	w := rr[start:]
	out := LiveHRV{SpanS: sum / 1000}
	if len(w) == 0 {
		return out
	}
	vals := make([]float64, len(w))
	for i, x := range w {
		vals[i] = x.MS
	}
	sorted := slices.Clone(vals)
	slices.Sort(sorted)
	med := sorted[len(sorted)/2]
	if len(sorted)%2 == 0 {
		med = (sorted[len(sorted)/2-1] + sorted[len(sorted)/2]) / 2
	}

	keep := make([]bool, len(w))
	var nn []float64
	var diffs []float64
	for i, v := range vals {
		keep[i] = v >= 300 && v <= 2000 && math.Abs(v-med) <= 0.25*med
		if !keep[i] {
			out.Excluded++
			continue
		}
		nn = append(nn, v)
		if i > 0 && keep[i-1] && !w[i].Break {
			diffs = append(diffs, v-vals[i-1])
		}
	}
	out.Beats = len(nn)
	var nnSum float64
	for _, v := range nn {
		nnSum += v
	}
	if len(nn) < liveMinBeats || nnSum/1000 < liveMinSpanS || len(diffs) < liveMinDiffs {
		return out
	}
	mean := nnSum / float64(len(nn))
	var ss, sd float64
	for _, v := range nn {
		ss += (v - mean) * (v - mean)
	}
	for _, d := range diffs {
		sd += d * d
	}
	out.SDNN = math.Sqrt(ss / float64(len(nn)-1))
	out.RMSSD = math.Sqrt(sd / float64(len(diffs)))
	out.OK = true
	return out
}

// appendRing appends to a bounded buffer, dropping the oldest entries.
func appendRing[T any](buf []T, limit int, xs ...T) []T {
	buf = append(buf, xs...)
	if over := len(buf) - limit; over > 0 {
		buf = append(buf[:0], buf[over:]...)
	}
	return buf
}
