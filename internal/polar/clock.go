package polar

// clockMap maps a sensor clock onto the host timeline: host = sensor + offset.
//
// Each observation pairs a sensor time with the host receive time of the
// packet that carried it. Their difference (raw offset) is the true offset
// plus transport latency, and latency is never negative, so the smallest raw
// offsets seen recently are the best estimate of the true offset. The map
// tracks the minimum raw offset over a sliding window and slews towards it by
// at most maxSlew per observation. This removes the initial anchor latency
// and follows drift between the sensor and host oscillators (tens of ppm)
// while keeping mapped times continuous and monotonic.
//
// If an observation's latency falls outside [minLat, maxLat] the mapping is
// considered broken (lost data, sensor clock reset) and is re-anchored.
type clockMap struct {
	maxSlew        int64
	minLat, maxLat int64

	anchored bool
	offset   int64
	win      []int64
	pos      int
}

func newClockMap(window int, maxSlew, minLat, maxLat int64) clockMap {
	return clockMap{maxSlew: maxSlew, minLat: minLat, maxLat: maxLat, win: make([]int64, window)}
}

// reset anchors the map so that the observation has zero latency.
func (c *clockMap) reset(raw int64) {
	c.anchored = true
	c.offset = raw
	for i := range c.win {
		c.win[i] = raw
	}
}

// observe records raw = hostReceive - sensorTime and adjusts the offset. It
// returns true if the map was (re)anchored.
func (c *clockMap) observe(raw int64) bool {
	if lat := raw - c.offset; !c.anchored || lat < c.minLat || lat > c.maxLat {
		c.reset(raw)
		return true
	}
	c.win[c.pos] = raw
	c.pos = (c.pos + 1) % len(c.win)
	target := c.win[0]
	for _, v := range c.win[1:] {
		target = min(target, v)
	}
	c.offset += max(-c.maxSlew, min(c.maxSlew, target-c.offset))
	return false
}
