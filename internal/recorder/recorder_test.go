package recorder

import (
	"bufio"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/al-bashkir/polar-h10/internal/polar"
	"github.com/al-bashkir/polar-h10/internal/storage"
)

func newSession(t *testing.T, start time.Time) *storage.Session {
	t.Helper()
	s, err := storage.Create(t.TempDir(), storage.Metadata{
		SessionID: "0192f3a4-1b2c-7def-8123-456789abcdef",
		StartedAt: start.UTC(),
		Device:    storage.DeviceMeta{ID: "ABC12345"},
	}, true)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func ecgPacket(at time.Time, devTs uint64, n int) polar.Packet {
	b := make([]byte, 10, 10+3*n)
	binary.LittleEndian.PutUint64(b[1:], devTs)
	for i := range n {
		v := int32(i*10 - 100)
		b = append(b, byte(v), byte(v>>8), byte(v>>16))
	}
	return polar.Packet{Received: at, Char: polar.PMDData, Data: b}
}

func hrPacket(at time.Time, hr byte, rr ...uint16) polar.Packet {
	b := []byte{0x16, hr}
	for _, v := range rr {
		b = binary.LittleEndian.AppendUint16(b, v)
	}
	return polar.Packet{Received: at, Char: polar.HeartRateMeasurement, Data: b}
}

func lines(t *testing.T, path string) []string {
	t.Helper()
	f, err := os.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	var out []string
	sc := bufio.NewScanner(f)
	for sc.Scan() {
		out = append(out, sc.Text())
	}
	return out
}

func eventTypes(t *testing.T, dir string) []string {
	var types []string
	for _, l := range lines(t, filepath.Join(dir, "events.jsonl")) {
		var e struct{ Type string }
		if err := json.Unmarshal([]byte(l), &e); err != nil {
			t.Fatalf("bad event line %q: %v", l, err)
		}
		types = append(types, e.Type)
	}
	return types
}

// Everything queued before Stop must be written: graceful shutdown drains.
func TestRecorderDrainsOnStop(t *testing.T) {
	start := time.Now()
	sess := newSession(t, start)
	r := newRecorder(start, sess, 100, nil)

	r.Packet(hrPacket(start.Add(time.Second), 72, 1024, 1000))
	r.Packet(hrPacket(start.Add(2*time.Second), 73, 980))
	r.Packet(ecgPacket(start.Add(time.Second), 10_000_000_000, 73))
	r.Packet(ecgPacket(start.Add(1500*time.Millisecond), 10_000_000_000+73*7_692_308, 73))
	r.Packet(polar.Packet{Received: start, Char: polar.PMDData, Data: []byte{0x00, 0x01}}) // malformed
	r.Packet(polar.Packet{Received: start, Char: polar.PMDControlPoint, TX: true, Data: []byte{0x02, 0x00}})

	go r.run()
	if err := r.Stop(); err != nil {
		t.Fatal(err)
	}
	st := r.Status().Stats
	if st.HRSamples != 2 || st.RRSamples != 3 || st.ECGSamples != 146 || st.ECGFrames != 2 ||
		st.DecodeErrors != 1 || st.BLEPackets != 5 || st.ECGGaps != 0 {
		t.Errorf("stats = %+v", st)
	}
	if err := sess.Close(r.Elapsed(start.Add(3 * time.Second))); err != nil {
		t.Fatal(err)
	}
	if n := len(lines(t, filepath.Join(sess.Dir, "ecg.csv"))); n != 147 {
		t.Errorf("ecg.csv has %d lines, want 147", n)
	}
	rr := lines(t, filepath.Join(sess.Dir, "rr.csv"))
	if len(rr) != 4 || !strings.HasSuffix(rr[1], ",1000") || !strings.HasSuffix(rr[2], ",976.5625") {
		t.Errorf("rr.csv = %v", rr)
	}
	if n := len(lines(t, filepath.Join(sess.Dir, "raw", "ble.jsonl"))); n != 6 {
		t.Errorf("raw has %d lines, want 6", n)
	}
	m, _ := storage.ReadMetadata(sess.Dir)
	if m.Statistics.ECGSamples != 146 || m.Statistics.DecodeErrors != 1 {
		t.Errorf("metadata statistics = %+v", m.Statistics)
	}
	types := strings.Join(eventTypes(t, sess.Dir), ",")
	if !strings.Contains(types, EvDecodeError) || !strings.Contains(types, EvECGClockAnchored) {
		t.Errorf("events = %s", types)
	}
}

// Overflowing the queue must be counted and reported, never silent.
func TestRecorderCountsDrops(t *testing.T) {
	start := time.Now()
	sess := newSession(t, start)
	r := newRecorder(start, sess, 2, nil)
	for range 5 {
		r.Packet(hrPacket(start, 60))
	}
	go r.run()
	r.Stop()
	sess.Meta.Statistics = r.Status().Stats
	sess.Close(1)

	if got := r.Status().Stats.DroppedPackets; got != 3 {
		t.Errorf("dropped = %d, want 3", got)
	}
	if got := r.Status().Stats.HRSamples; got != 2 {
		t.Errorf("hr samples = %d, want 2", got)
	}
	var found bool
	for _, l := range lines(t, filepath.Join(sess.Dir, "events.jsonl")) {
		if strings.Contains(l, `"type":"packets_dropped"`) && strings.Contains(l, `"count":3`) {
			found = true
		}
	}
	if !found {
		t.Error("packets_dropped event missing")
	}
	m, _ := storage.ReadMetadata(sess.Dir)
	if m.Statistics.DroppedPackets != 3 {
		t.Errorf("metadata dropped = %d", m.Statistics.DroppedPackets)
	}
}

func TestRecorderECGGapReported(t *testing.T) {
	start := time.Now()
	sess := newSession(t, start)
	r := newRecorder(start, sess, 10, nil)
	r.Packet(ecgPacket(start.Add(time.Second), 1_000_000_000, 73))
	// 73 samples missing between frames.
	r.Packet(ecgPacket(start.Add(2*time.Second), 1_000_000_000+146*7_692_308, 73))
	go r.run()
	r.Stop()
	st := r.Status().Stats
	if st.ECGGaps != 1 || st.ECGMissingSamplesEst != 73 {
		t.Errorf("stats = %+v", st)
	}
	sess.Close(1)
	if !strings.Contains(strings.Join(eventTypes(t, sess.Dir), ","), EvECGGap) {
		t.Error("ecg_gap event missing")
	}
}

func TestRecorderWriteFailure(t *testing.T) {
	start := time.Now()
	sess := newSession(t, start)
	sess.Close(0) // any further write fails
	r := New(start, sess, 10, nil)
	r.Packet(hrPacket(start, 60))
	select {
	case <-r.Failed():
	case <-time.After(2 * time.Second):
		t.Fatal("write failure not signalled")
	}
	if err := r.Stop(); err == nil {
		t.Error("Stop returned nil after write failure")
	}
}

func TestMonitorWithoutSession(t *testing.T) {
	start := time.Now()
	r := New(start, nil, 10, nil)
	r.Event(EvDeviceConnected, nil)
	r.Event(EvBatteryLevel, map[string]any{"percent": 87})
	r.Packet(hrPacket(start, 72, 1024))
	if err := r.Stop(); err != nil {
		t.Fatal(err)
	}
	s := r.Status()
	if !s.Connected || s.HR != 72 || s.RRMs != 1000 || s.Battery != 87 || s.Contact == nil || !*s.Contact {
		t.Errorf("status = %+v", s)
	}
}

// fakeLink is a minimal H10 stand-in for acquisition tests.
type fakeLink struct {
	mu        sync.Mutex
	connected bool
	cp        func([]byte)
}

func (f *fakeLink) Has(string) bool { return true }
func (f *fakeLink) Read(c string) ([]byte, error) {
	switch c {
	case polar.PMDControlPoint:
		return []byte{0x0f, 0x05}, nil
	case polar.BatteryLevel:
		return []byte{80}, nil
	}
	return []byte("x"), nil
}
func (f *fakeLink) Write(c string, d []byte) error {
	f.mu.Lock()
	cp := f.cp
	f.mu.Unlock()
	go cp([]byte{0xf0, d[0], d[1], 0, 0})
	return nil
}
func (f *fakeLink) Subscribe(c string, fn func([]byte)) error {
	if c == polar.PMDControlPoint {
		f.mu.Lock()
		f.cp = fn
		f.mu.Unlock()
	}
	return nil
}
func (f *fakeLink) Connected() bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.connected
}
func (f *fakeLink) Close() error {
	f.mu.Lock()
	f.connected = false
	f.mu.Unlock()
	return nil
}

func TestAcquireReconnects(t *testing.T) {
	start := time.Now()
	sess := newSession(t, start)
	r := New(start, sess, 100, nil)

	first := &fakeLink{connected: true}
	dev, err := polar.Open(first, "Polar H10 ABC12345", "", r.Packet)
	if err != nil {
		t.Fatal(err)
	}
	dials := 0
	dial := func(ctx context.Context, sink func(polar.Packet)) (*polar.Device, error) {
		dials++
		if dials == 1 {
			return nil, errors.New("not found")
		}
		return polar.Open(&fakeLink{connected: true}, "Polar H10 ABC12345", "", sink)
	}
	opt := AcquireOptions{ECG: true, ReconnectAttempts: 3, ReconnectDelay: time.Millisecond,
		PollInterval: 5 * time.Millisecond, BatteryInterval: time.Hour}

	ctx, cancel := context.WithCancel(context.Background())
	go func() {
		time.Sleep(30 * time.Millisecond)
		first.Close() // link drops
		for r.Status().Stats.Reconnections == 0 {
			time.Sleep(5 * time.Millisecond)
		}
		cancel()
	}()
	if err := Acquire(ctx, dev, r, dial, opt); err != nil {
		t.Fatal(err)
	}
	r.Stop()
	if b := r.Status().Battery; b != 80 {
		t.Errorf("final battery = %d", b)
	}
	sess.Close(1)
	got := strings.Join(eventTypes(t, sess.Dir), ",")
	want := "hr_stream_started,ecg_stream_started,connection_lost,reconnect_attempt,reconnect_failed," +
		"reconnect_attempt,hr_stream_started,ecg_stream_started,device_reconnected,ecg_stream_stopped,battery_level"
	if got != want {
		t.Errorf("events:\n got %s\nwant %s", got, want)
	}
}

func TestAcquireGivesUp(t *testing.T) {
	start := time.Now()
	r := New(start, nil, 100, nil)
	link := &fakeLink{connected: true}
	dev, _ := polar.Open(link, "Polar H10 X", "", r.Packet)
	link.Close()
	dial := func(context.Context, func(polar.Packet)) (*polar.Device, error) { return nil, errors.New("gone") }
	opt := AcquireOptions{ReconnectAttempts: 2, ReconnectDelay: time.Millisecond,
		PollInterval: time.Millisecond, BatteryInterval: time.Hour}
	if err := Acquire(context.Background(), dev, r, dial, opt); !errors.Is(err, ErrDeviceLost) {
		t.Errorf("err = %v, want ErrDeviceLost", err)
	}
	r.Stop()
	if st := r.Status().Stats; st.Disconnects != 1 || st.Reconnections != 0 {
		t.Errorf("stats = %+v", st)
	}
}
