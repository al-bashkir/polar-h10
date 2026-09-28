package storage

import (
	"encoding/csv"
	"encoding/json"
	"maps"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"slices"
	"strings"
	"testing"
	"time"
)

var t0 = time.Date(2026, 9, 28, 3, 32, 14, 123456000, time.UTC)

func newTestSession(t *testing.T, raw bool) *Session {
	t.Helper()
	s, err := Create(t.TempDir(), Metadata{
		SessionID: "0192f3a4-1b2c-7def-8123-456789abcdef",
		Name:      "morning-rest",
		StartedAt: t0,
		Device:    DeviceMeta{Name: "Polar H10 ABC12345", ID: "ABC12345"},
	}, raw)
	if err != nil {
		t.Fatal(err)
	}
	return s
}

func readFile(t *testing.T, path string) string {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func TestSessionID(t *testing.T) {
	id, err := NewSessionID(t0)
	if err != nil {
		t.Fatal(err)
	}
	if !regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`).MatchString(id) {
		t.Errorf("not a UUIDv7: %s", id)
	}
	id2, _ := NewSessionID(t0)
	if id == id2 {
		t.Error("ids collide")
	}
}

func TestDirName(t *testing.T) {
	got := DirName(t0, "ABC12345", "0192f3a4-1b2c-7def-8123-456789abcdef")
	if got != "2026-09-28T03-32-14Z_ABC12345_89abcdef" {
		t.Errorf("DirName = %s", got)
	}
	if got := DirName(t0, "../x y", "abc"); got != "2026-09-28T03-32-14Z_..xy_abc" {
		t.Errorf("unsafe id not sanitized: %s", got)
	}
}

func TestCreateLayout(t *testing.T) {
	s := newTestSession(t, true)
	for _, f := range []string{"metadata.json", "hr.csv", "rr.csv", "ecg.csv", "events.jsonl", "raw/ble.jsonl"} {
		if _, err := os.Stat(filepath.Join(s.Dir, f)); err != nil {
			t.Errorf("missing %s: %v", f, err)
		}
	}
	m, err := ReadMetadata(s.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if m.Status != StatusRecording || m.SchemaVersion != SchemaVersion || m.EndedAt != nil {
		t.Errorf("initial metadata = %+v", m)
	}
	// Same directory cannot be created twice.
	if _, err := Create(filepath.Dir(s.Dir), s.Meta, false); err == nil {
		t.Error("session directory collision not detected")
	}
	s.Close(0)
}

func TestNoRaw(t *testing.T) {
	s := newTestSession(t, false)
	if _, err := os.Stat(filepath.Join(s.Dir, "raw")); !os.IsNotExist(err) {
		t.Error("raw directory created with raw disabled")
	}
	if err := s.WriteRaw(t0, 0, "x", "x", false, []byte{1}); err != nil {
		t.Error(err)
	}
	s.Close(0)
}

func TestCSVOutput(t *testing.T) {
	s := newTestSession(t, true)
	s.WriteHR(0, 72)
	s.WriteHR(1_001_467_000, 73)
	s.WriteRR(708_644_000, 832.03125)
	s.WriteRR(1_542_536_000, 1000)
	s.WriteECG(0, 0, 124)
	s.WriteECG(7_692_307, 1, -128)
	s.WriteRaw(t0.Add(time.Second), 1_000_000_000, "00002a37-0000-1000-8000-00805f9b34fb", "hr_measurement", false, []byte{0x16, 0x48})
	if err := s.Close(2_000_000_000); err != nil {
		t.Fatal(err)
	}

	checks := map[string]string{
		"hr.csv": "timestamp,elapsed_ns,heart_rate_bpm\n" +
			"2026-09-28T03:32:14.123456000Z,0,72\n" +
			"2026-09-28T03:32:15.124923000Z,1001467000,73\n",
		"rr.csv": "timestamp,elapsed_ns,rr_ms\n" +
			"2026-09-28T03:32:14.832100000Z,708644000,832.03125\n" +
			"2026-09-28T03:32:15.665992000Z,1542536000,1000\n",
		"ecg.csv": "timestamp,elapsed_ns,sample_index,ecg_uv\n" +
			"2026-09-28T03:32:14.123456000Z,0,0,124\n" +
			"2026-09-28T03:32:14.131148307Z,7692307,1,-128\n",
		"raw/ble.jsonl": `{"received_at":"2026-09-28T03:32:15.123456000Z","elapsed_ns":1000000000,"direction":"rx",` +
			`"characteristic":"00002a37-0000-1000-8000-00805f9b34fb","name":"hr_measurement","data":"1648"}` + "\n",
	}
	for name, want := range checks {
		if got := readFile(t, filepath.Join(s.Dir, name)); got != want {
			t.Errorf("%s:\n%s\nwant:\n%s", name, got, want)
		}
	}
	// Files parse as regular CSV.
	for _, name := range []string{"hr.csv", "rr.csv", "ecg.csv"} {
		if _, err := csv.NewReader(strings.NewReader(readFile(t, filepath.Join(s.Dir, name)))).ReadAll(); err != nil {
			t.Errorf("%s: %v", name, err)
		}
	}
	var raw map[string]any
	if err := json.Unmarshal([]byte(checks["raw/ble.jsonl"]), &raw); err != nil {
		t.Error(err)
	}
}

func TestEventEncoding(t *testing.T) {
	b, err := EncodeEvent(t0, 5, "connection_lost", map[string]any{"error": "x \"y\"", "attempt": 2, "type": "ignored"})
	if err != nil {
		t.Fatal(err)
	}
	want := `{"timestamp":"2026-09-28T03:32:14.123456000Z","elapsed_ns":5,"type":"connection_lost","attempt":2,"error":"x \"y\""}` + "\n"
	if string(b) != want {
		t.Errorf("got  %s\nwant %s", b, want)
	}
	var v map[string]any
	if err := json.Unmarshal(b, &v); err != nil {
		t.Error(err)
	}
}

func TestFinalMetadata(t *testing.T) {
	s := newTestSession(t, false)
	s.Meta.StopReason = "sigint"
	s.Meta.Statistics.ECGSamples = 79560
	if err := s.Close(611_870_000_000); err != nil {
		t.Fatal(err)
	}
	m, err := ReadMetadata(s.Dir)
	if err != nil {
		t.Fatal(err)
	}
	if m.Status != StatusCompleted || m.StopReason != "sigint" || m.Statistics.ECGSamples != 79560 {
		t.Errorf("final metadata = %+v", m)
	}
	if !m.EndedAt.Equal(t0.Add(611870*time.Millisecond)) || *m.DurationSeconds != 611.87 {
		t.Errorf("end = %v duration = %v", m.EndedAt, *m.DurationSeconds)
	}
	// No temp files left, data read-only.
	entries, _ := os.ReadDir(s.Dir)
	for _, e := range entries {
		if strings.HasPrefix(e.Name(), ".metadata-") {
			t.Errorf("temp file left: %s", e.Name())
		}
	}
	fi, _ := os.Stat(filepath.Join(s.Dir, "hr.csv"))
	if fi.Mode().Perm()&0o222 != 0 {
		t.Errorf("hr.csv still writable: %v", fi.Mode())
	}
	// Writes after close fail rather than silently succeeding.
	if err := s.WriteHR(0, 1); err == nil {
		t.Error("write after close succeeded")
	}
}

// TestMetadataSchema pins the JSON field names of metadata.json: changing
// them is a schema change and requires bumping SchemaVersion.
func TestMetadataSchema(t *testing.T) {
	b, _ := json.Marshal(Metadata{})
	var top map[string]json.RawMessage
	json.Unmarshal(b, &top)
	want := []string{"application", "device", "duration_seconds", "ended_at", "host", "name", "recording",
		"schema_version", "session_id", "started_at", "statistics", "status", "timezone", "timing", "utc_offset"}
	got := slices.Sorted(maps.Keys(top))
	if !reflect.DeepEqual(got, want) {
		t.Errorf("metadata fields = %v\nwant %v", got, want)
	}
	var stats map[string]any
	json.Unmarshal(top["statistics"], &stats)
	for _, k := range []string{"hr_samples", "rr_samples", "ecg_samples", "ble_packets", "decode_errors", "dropped_packets", "reconnections"} {
		if _, ok := stats[k]; !ok {
			t.Errorf("statistics.%s missing", k)
		}
	}
	if SchemaVersion != 1 {
		t.Error("schema version changed: update docs/data-format.md")
	}
	if HRHeader != "timestamp,elapsed_ns,heart_rate_bpm" || RRHeader != "timestamp,elapsed_ns,rr_ms" ||
		ECGHeader != "timestamp,elapsed_ns,sample_index,ecg_uv" {
		t.Error("CSV headers changed: this is a schema change")
	}
}

func TestListSessions(t *testing.T) {
	dir := t.TempDir()
	for i, name := range []string{"a", "b"} {
		s, err := Create(dir, Metadata{SessionID: name, Name: name, StartedAt: t0.Add(time.Duration(i) * time.Hour),
			Device: DeviceMeta{ID: "ABC12345"}}, false)
		if err != nil {
			t.Fatal(err)
		}
		s.Close(1)
	}
	os.Mkdir(filepath.Join(dir, "not-a-session"), 0o755)
	list, err := ListSessions(dir)
	if err != nil {
		t.Fatal(err)
	}
	if len(list) != 2 || list[0].Meta.Name != "b" {
		t.Errorf("list = %+v", list)
	}
	if l, err := ListSessions(filepath.Join(dir, "missing")); err != nil || l != nil {
		t.Errorf("missing dir: %v %v", l, err)
	}
}
