// Package recorder turns raw BLE packets into timestamped samples, keeps
// counters and live status, and (when recording) writes everything to a
// storage.Session from a single goroutine.
package recorder

import (
	"encoding/hex"
	"log/slog"
	"sync"
	"sync/atomic"
	"time"

	"github.com/al-bashkir/polar-h10/internal/polar"
	"github.com/al-bashkir/polar-h10/internal/storage"
)

// Event types written to events.jsonl.
const (
	EvSessionStarted    = "session_started"
	EvSessionStopped    = "session_stopped"
	EvDeviceConnected   = "device_connected"
	EvConnectionLost    = "connection_lost"
	EvReconnectAttempt  = "reconnect_attempt"
	EvReconnectFailed   = "reconnect_failed"
	EvDeviceReconnected = "device_reconnected"
	EvHRStreamStarted   = "hr_stream_started"
	EvECGStreamStarted  = "ecg_stream_started"
	EvECGStreamStopped  = "ecg_stream_stopped"
	EvECGClockAnchored  = "ecg_clock_anchored"
	EvECGClockRebased   = "ecg_clock_rebased"
	EvECGGap            = "ecg_gap"
	EvRRChainRebased    = "rr_chain_rebased"
	EvSensorContact     = "sensor_contact_changed"
	EvBatteryLevel      = "battery_level"
	EvPacketsDropped    = "packets_dropped"
	EvDecodeError       = "decode_error"
	EvDataStalled       = "data_stalled"
	EvWriteError        = "write_error"
)

// Tunables.
const (
	DefaultQueueSize   = 4096 // packets; ~30 minutes of HR+ECG at H10 rates would never be queued at once
	flushEvery         = time.Second
	checkpointEvery    = 30 * time.Second
	maxDecodeErrEvents = 100
)

// Status is a snapshot of live acquisition state for display.
type Status struct {
	Connected    bool
	HR           int
	HRAt         time.Time
	RRMs         float64
	Contact      *bool
	Battery      int // -1 unknown
	ECGStreaming bool
	LastECG      time.Time
	LastPacket   time.Time
	Stats        storage.Statistics
	Err          error
}

// Recorder consumes packets and events. Packet may be called from any
// goroutine (typically BLE callbacks) and never blocks; everything else is
// processed in order by one internal goroutine, which is also the only
// writer to the session.
type Recorder struct {
	start time.Time
	sess  *storage.Session
	log   *slog.Logger

	in      chan item
	quit    chan struct{}
	done    chan struct{}
	failed  chan struct{}
	stopped atomic.Bool
	dropped atomic.Int64

	mu     sync.Mutex
	status Status

	// Owned by the run goroutine.
	ecg            *polar.ECGTimeline
	rr             *polar.RRTimeline
	rrAnchored     bool
	reportedDrops  int64
	lastCheckpoint time.Time
	ecgBuf         []polar.ECGSample
	rrBuf          []polar.RRInterval
	err            error
}

type item struct {
	pkt polar.Packet
	ev  *event
}

type event struct {
	at     time.Time
	typ    string
	fields map[string]any
}

// New starts a recorder. start is the session start (with a monotonic clock
// reading); sess may be nil for live monitoring without storage.
func New(start time.Time, sess *storage.Session, queueSize int, log *slog.Logger) *Recorder {
	r := newRecorder(start, sess, queueSize, log)
	go r.run()
	return r
}

func newRecorder(start time.Time, sess *storage.Session, queueSize int, log *slog.Logger) *Recorder {
	if queueSize <= 0 {
		queueSize = DefaultQueueSize
	}
	if log == nil {
		log = slog.New(slog.DiscardHandler)
	}
	r := &Recorder{
		start:          start,
		sess:           sess,
		log:            log,
		in:             make(chan item, queueSize),
		quit:           make(chan struct{}),
		done:           make(chan struct{}),
		failed:         make(chan struct{}),
		ecg:            polar.NewECGTimeline(polar.ECGSampleRate),
		rr:             polar.NewRRTimeline(),
		lastCheckpoint: start,
	}
	r.status.Battery = -1
	return r
}

// Elapsed returns ns since session start using the monotonic clock.
func (r *Recorder) Elapsed(t time.Time) int64 { return t.Sub(r.start).Nanoseconds() }

// Packet queues a raw packet without blocking. If the queue is full the
// packet is counted as dropped and reported as an event.
func (r *Recorder) Packet(p polar.Packet) {
	if r.stopped.Load() {
		return
	}
	select {
	case r.in <- item{pkt: p}:
	default:
		r.dropped.Add(1)
	}
}

// Event records a session event. It blocks until the event is queued (the
// queue drains continuously) or the recorder has stopped.
func (r *Recorder) Event(typ string, fields map[string]any) {
	ev := &event{at: time.Now(), typ: typ, fields: fields}
	attrs := []any{"type", typ}
	for k, v := range fields {
		attrs = append(attrs, k, v)
	}
	r.log.Info("event", attrs...)
	select {
	case r.in <- item{ev: ev}:
	case <-r.done:
	}
}

// Failed is closed when writing to storage fails; the recording must stop.
func (r *Recorder) Failed() <-chan struct{} { return r.failed }

// Status returns a snapshot of the live state.
func (r *Recorder) Status() Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	s := r.status
	s.Stats.DroppedPackets = r.dropped.Load()
	return s
}

// Stop processes everything already queued, flushes storage and stops the
// recorder. It returns the first storage error, if any. The session itself
// is left open for the caller to finalize.
func (r *Recorder) Stop() error {
	if r.stopped.CompareAndSwap(false, true) {
		close(r.quit)
	}
	<-r.done
	return r.err
}

func (r *Recorder) run() {
	defer close(r.done)
	tick := time.NewTicker(flushEvery)
	defer tick.Stop()
	for {
		select {
		case it := <-r.in:
			r.handle(it)
		case now := <-tick.C:
			r.periodic(now)
		case <-r.quit:
			for len(r.in) > 0 {
				r.handle(<-r.in)
			}
			r.reportDrops(time.Now())
			if r.sess != nil {
				r.sess.Meta.Statistics = r.Status().Stats
				r.check(r.sess.Flush())
			}
			return
		}
	}
}

func (r *Recorder) handle(it item) {
	if it.ev != nil {
		r.handleEvent(it.ev)
		return
	}
	p := it.pkt
	elapsed := r.Elapsed(p.Received)
	if r.sess != nil {
		r.check(r.sess.WriteRaw(p.Received, elapsed, p.Char, polar.CharName(p.Char), p.TX, p.Data))
	}
	if p.TX {
		return
	}
	r.mu.Lock()
	r.status.Stats.BLEPackets++
	r.status.LastPacket = p.Received
	r.mu.Unlock()

	switch p.Char {
	case polar.HeartRateMeasurement:
		r.handleHR(p, elapsed)
	case polar.PMDData:
		if len(p.Data) > 0 && p.Data[0] != polar.MeasECG {
			return // other measurement types are not requested
		}
		r.handleECG(p, elapsed)
	}
}

func (r *Recorder) decodeError(p polar.Packet, err error) {
	r.mu.Lock()
	r.status.Stats.DecodeErrors++
	n := r.status.Stats.DecodeErrors
	r.mu.Unlock()
	r.log.Warn("decode error", "characteristic", polar.CharName(p.Char), "err", err)
	if n <= maxDecodeErrEvents {
		r.writeEvent(p.Received, EvDecodeError, map[string]any{
			"characteristic": polar.CharName(p.Char),
			"error":          err.Error(),
			"data":           hex.EncodeToString(p.Data),
			"count":          n,
		})
	}
}

func (r *Recorder) handleHR(p polar.Packet, elapsed int64) {
	m, err := polar.ParseHR(p.Data)
	if err != nil {
		r.decodeError(p, err)
		return
	}
	var rebased bool
	r.rrBuf, rebased = r.rr.Place(m.RR, elapsed, r.rrBuf[:0])

	rrRebased := rebased && r.rrAnchored
	r.rrAnchored = r.rrAnchored || rebased

	r.mu.Lock()
	r.status.HR = m.HeartRate
	r.status.HRAt = p.Received
	r.status.Stats.HRSamples++
	r.status.Stats.RRSamples += int64(len(r.rrBuf))
	if len(r.rrBuf) > 0 {
		r.status.RRMs = polar.RRMillis(r.rrBuf[len(r.rrBuf)-1].Raw)
	}
	// Report contact changes, and a missing contact when first seen.
	var contactChanged bool
	if m.ContactSupported {
		prev := r.status.Contact
		contactChanged = prev == nil && !m.ContactDetected || prev != nil && *prev != m.ContactDetected
		c := m.ContactDetected
		r.status.Contact = &c
	}
	if rrRebased {
		r.status.Stats.RRChainRebases++
	}
	r.mu.Unlock()

	if contactChanged {
		r.writeEvent(p.Received, EvSensorContact, map[string]any{"contact": m.ContactDetected})
	}
	if rrRebased {
		r.writeEvent(p.Received, EvRRChainRebased, map[string]any{"reason": "rr_chain_diverged_from_receive_time"})
	}
	if r.sess == nil {
		return
	}
	r.check(r.sess.WriteHR(elapsed, m.HeartRate))
	for _, iv := range r.rrBuf {
		r.check(r.sess.WriteRR(iv.Elapsed, polar.RRMillis(iv.Raw)))
	}
}

func (r *Recorder) handleECG(p polar.Packet, elapsed int64) {
	f, err := polar.ParseECGFrame(p.Data)
	if err != nil {
		r.decodeError(p, err)
		return
	}
	var pl polar.Placement
	r.ecgBuf, pl = r.ecg.Place(f, elapsed, r.ecgBuf[:0])

	r.mu.Lock()
	r.status.Stats.ECGFrames++
	r.status.Stats.ECGSamples += int64(len(r.ecgBuf))
	r.status.LastECG = p.Received
	r.status.ECGStreaming = true
	if pl.MissingSamples > 0 {
		r.status.Stats.ECGGaps++
		r.status.Stats.ECGMissingSamplesEst += pl.MissingSamples
	}
	if pl.Rebased != "" && pl.Rebased != "initial" {
		r.status.Stats.ECGClockRebases++
	}
	r.mu.Unlock()

	switch pl.Rebased {
	case "":
	case "initial":
		r.writeEvent(p.Received, EvECGClockAnchored, map[string]any{
			"device_timestamp_ns": f.DeviceTimestamp, "anchor_elapsed_ns": elapsed,
		})
	default:
		r.writeEvent(p.Received, EvECGClockRebased, map[string]any{
			"reason": pl.Rebased, "device_timestamp_ns": f.DeviceTimestamp, "anchor_elapsed_ns": elapsed,
		})
	}
	if pl.MissingSamples > 0 {
		r.log.Warn("ecg gap", "missing_samples", pl.MissingSamples)
		r.writeEvent(p.Received, EvECGGap, map[string]any{
			"missing_samples_estimated": pl.MissingSamples,
			"gap_ns":                    pl.MissingSamples * pl.PeriodNs,
			"next_sample_index":         r.ecgBuf[0].Index,
		})
	}
	if r.sess == nil {
		return
	}
	for _, s := range r.ecgBuf {
		r.check(r.sess.WriteECG(s.Elapsed, s.Index, s.Microvolts))
	}
}

func (r *Recorder) handleEvent(ev *event) {
	r.mu.Lock()
	switch ev.typ {
	case EvDeviceConnected, EvDeviceReconnected:
		r.status.Connected = true
		if ev.typ == EvDeviceReconnected {
			r.status.Stats.Reconnections++
		}
	case EvConnectionLost:
		r.status.Connected = false
		r.status.ECGStreaming = false
		r.status.Stats.Disconnects++
	case EvECGStreamStopped:
		r.status.ECGStreaming = false
	case EvBatteryLevel:
		if v, ok := ev.fields["percent"].(int); ok {
			r.status.Battery = v
		}
	}
	r.mu.Unlock()
	r.writeEvent(ev.at, ev.typ, ev.fields)
}

func (r *Recorder) writeEvent(at time.Time, typ string, fields map[string]any) {
	if r.sess != nil {
		r.check(r.sess.WriteEvent(r.Elapsed(at), typ, fields))
	}
}

func (r *Recorder) reportDrops(now time.Time) {
	total := r.dropped.Load()
	if total == r.reportedDrops {
		return
	}
	delta := total - r.reportedDrops
	r.reportedDrops = total
	r.log.Warn("packets dropped: queue full", "count", delta, "total", total)
	r.writeEvent(now, EvPacketsDropped, map[string]any{"count": delta, "total": total, "reason": "queue_full"})
}

func (r *Recorder) periodic(now time.Time) {
	r.reportDrops(now)
	if r.sess == nil {
		return
	}
	if now.Sub(r.lastCheckpoint) >= checkpointEvery {
		r.lastCheckpoint = now
		r.sess.Meta.Statistics = r.Status().Stats
		r.check(r.sess.Checkpoint())
	} else {
		r.check(r.sess.Flush())
	}
}

// check records the first storage error and signals failure.
func (r *Recorder) check(err error) {
	if err == nil || r.err != nil {
		return
	}
	r.err = err
	r.log.Error("storage write failed; stopping recording", "err", err)
	r.mu.Lock()
	r.status.Err = err
	r.mu.Unlock()
	close(r.failed)
}
