package polar

import (
	"bytes"
	"errors"
	"sync"
	"testing"
	"time"
)

// fakeLink emulates a Polar H10 GATT server.
type fakeLink struct {
	mu        sync.Mutex
	values    map[string][]byte
	subs      map[string]func([]byte)
	writes    [][]byte
	respond   func(cmd []byte) []byte // control point response, nil = none
	connected bool
}

func newFakeH10() *fakeLink {
	return &fakeLink{
		connected: true,
		values: map[string][]byte{
			HeartRateMeasurement: nil,
			BatteryLevel:         {87},
			ManufacturerName:     []byte("Polar Electro Oy"),
			ModelNumber:          []byte("H10"),
			FirmwareRevision:     []byte("3.1.1\x00"),
			PMDControlPoint:      {0x0f, 0x05, 0x00}, // ecg + acc
			PMDData:              nil,
		},
		subs: map[string]func([]byte){},
		respond: func(cmd []byte) []byte {
			switch cmd[0] {
			case 0x01:
				return []byte{0xf0, 0x01, cmd[1], 0x00, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0e, 0x00}
			default:
				return []byte{0xf0, cmd[0], cmd[1], 0x00, 0x00}
			}
		},
	}
}

func (f *fakeLink) Has(c string) bool { _, ok := f.values[c]; return ok }
func (f *fakeLink) Read(c string) ([]byte, error) {
	v, ok := f.values[c]
	if !ok {
		return nil, errors.New("no such characteristic")
	}
	return v, nil
}
func (f *fakeLink) Write(c string, data []byte) error {
	f.mu.Lock()
	f.writes = append(f.writes, append([]byte(nil), data...))
	cb := f.subs[PMDControlPoint]
	f.mu.Unlock()
	if c == PMDControlPoint && cb != nil && f.respond != nil {
		if r := f.respond(data); r != nil {
			go cb(r) // indications arrive asynchronously
		}
	}
	return nil
}
func (f *fakeLink) Subscribe(c string, fn func([]byte)) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.subs[c] = fn
	return nil
}
func (f *fakeLink) Connected() bool { return f.connected }
func (f *fakeLink) Close() error    { f.connected = false; return nil }

func TestOpenReadsInfo(t *testing.T) {
	d, err := Open(newFakeH10(), "Polar H10 ABC12345", "addr", nil)
	if err != nil {
		t.Fatal(err)
	}
	i := d.Info
	if i.ID != "ABC12345" || i.Battery != 87 || i.Firmware != "3.1.1" || i.Model != "H10" {
		t.Errorf("info = %+v", i)
	}
	if !i.Features[MeasECG] || !i.Features[MeasACC] || i.Features[MeasPPG] {
		t.Errorf("features = %v", i.Features)
	}
	if i.Features.String() != "ecg,acc" {
		t.Errorf("features string = %q", i.Features.String())
	}
}

func TestStartECG(t *testing.T) {
	link := newFakeH10()
	var mu sync.Mutex
	var pkts []Packet
	d, err := Open(link, "Polar H10 ABC12345", "", func(p Packet) {
		mu.Lock()
		pkts = append(pkts, p)
		mu.Unlock()
	})
	if err != nil {
		t.Fatal(err)
	}
	s, err := d.ECGSettings()
	if err != nil {
		t.Fatal(err)
	}
	if s[SettingSampleRate][0] != 130 || s[SettingResolution][0] != 14 {
		t.Errorf("settings = %v", s)
	}
	if err := d.StartECG(); err != nil {
		t.Fatal(err)
	}
	want := []byte{0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0e, 0x00}
	if got := link.writes[len(link.writes)-1]; !bytes.Equal(got, want) {
		t.Errorf("start command = % x, want % x", got, want)
	}
	// Data notifications reach the sink.
	link.subs[PMDData](ecgFrame(1, 1, 2))
	mu.Lock()
	defer mu.Unlock()
	var tx, data int
	for _, p := range pkts {
		if p.TX {
			tx++
		}
		if p.Char == PMDData {
			data++
		}
	}
	if tx != 2 || data != 1 {
		t.Errorf("tx=%d data=%d packets, want 2 and 1", tx, data)
	}
}

func TestStartECGErrors(t *testing.T) {
	link := newFakeH10()
	link.respond = func(cmd []byte) []byte { return []byte{0xf0, cmd[0], cmd[1], 0x0d, 0x00} }
	d, _ := Open(link, "Polar H10 X", "", nil)
	err := d.StartECG()
	var pe *PMDError
	if !errors.As(err, &pe) || pe.Status != 13 {
		t.Fatalf("err = %v, want device in charger", err)
	}

	link.respond = func(cmd []byte) []byte { return []byte{0xf0, cmd[0], cmd[1], 0x06, 0x00} }
	if err := d.StartECG(); err != nil {
		t.Errorf("already streaming should succeed, got %v", err)
	}

	old := cpTimeout
	cpTimeout = 50 * time.Millisecond
	defer func() { cpTimeout = old }()
	link.respond = nil
	if err := d.StartECG(); err == nil {
		t.Error("expected timeout error")
	}

	link.values[PMDControlPoint] = []byte{0x0f, 0x04} // acc only
	d, _ = Open(link, "Polar H10 X", "", nil)
	if err := d.StartECG(); !errors.Is(err, ErrNotSupported) {
		t.Errorf("err = %v, want ErrNotSupported", err)
	}
}

func TestParseCPResponseAndSettings(t *testing.T) {
	if _, err := ParseCPResponse([]byte{0xf0, 0x02}); err == nil {
		t.Error("short response accepted")
	}
	if _, err := ParseCPResponse([]byte{0x0f, 0x02, 0x00, 0x00}); err == nil {
		t.Error("wrong header accepted")
	}
	if _, err := ParseSettings([]byte{0x00, 0x02, 0x82}); err == nil {
		t.Error("truncated settings accepted")
	}
	if _, err := ParseSettings([]byte{0x09, 0x01, 0x00}); err == nil {
		t.Error("unknown setting type accepted")
	}
	if _, err := ParseFeatures([]byte{0x0f}); err == nil {
		t.Error("short features accepted")
	}
}

func TestDeviceID(t *testing.T) {
	if !IsH10("Polar H10 ABC12345") || IsH10("Polar OH1 1234") || IsH10("Polar H100") {
		t.Error("IsH10 mismatch")
	}
	if DeviceID("Polar H10 ABC12345") != "ABC12345" || DeviceID("Polar") != "" {
		t.Error("DeviceID mismatch")
	}
}
