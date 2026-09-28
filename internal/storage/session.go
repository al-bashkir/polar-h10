package storage

import (
	"bufio"
	"crypto/rand"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"time"
)

// CSV headers. These are part of the file format: see docs/data-format.md.
const (
	HRHeader  = "timestamp,elapsed_ns,heart_rate_bpm"
	RRHeader  = "timestamp,elapsed_ns,rr_ms"
	ECGHeader = "timestamp,elapsed_ns,sample_index,ecg_uv"
)

// TimeFormat is used for every stored timestamp: UTC, fixed 9 fractional
// digits, so values sort lexically and parse with any RFC 3339 parser.
const TimeFormat = "2006-01-02T15:04:05.000000000Z"

// Session is an open recording directory. Its methods are not safe for
// concurrent use: a single writer goroutine owns it.
//
// The first write error is sticky: all later writes fail with it, so callers
// can check any write (or Flush) and stop the recording.
type Session struct {
	Dir  string
	Meta Metadata

	start time.Time // wall clock base (UTC) for started_at + elapsed

	hr, rr, ecg, events, raw *file

	err    error
	closed bool
}

type file struct {
	f *os.File
	w *bufio.Writer
}

// NewSessionID returns a UUIDv7 (time-ordered, random) as a string.
func NewSessionID(now time.Time) (string, error) {
	var u [16]byte
	if _, err := rand.Read(u[6:]); err != nil {
		return "", err
	}
	var ms [8]byte
	binary.BigEndian.PutUint64(ms[:], uint64(now.UnixMilli()))
	copy(u[:6], ms[2:])
	u[6] = 0x70 | u[6]&0x0f // version 7
	u[8] = 0x80 | u[8]&0x3f // RFC 4122 variant
	h := hex.EncodeToString(u[:])
	return h[0:8] + "-" + h[8:12] + "-" + h[12:16] + "-" + h[16:20] + "-" + h[20:], nil
}

var unsafeChars = regexp.MustCompile(`[^A-Za-z0-9._-]+`)

// DirName returns the session directory name
// YYYY-MM-DDTHH-MM-SSZ_<device-id>_<short-id>, where short-id is the last 8
// hex digits (random bits) of the session UUID.
func DirName(start time.Time, deviceID, sessionID string) string {
	id := unsafeChars.ReplaceAllString(deviceID, "")
	if id == "" {
		id = "unknown"
	}
	short := sessionID
	if len(short) > 8 {
		short = short[len(short)-8:]
	}
	return start.UTC().Format("2006-01-02T15-04-05Z") + "_" + id + "_" + short
}

// Create makes a new session directory under dataDir and opens its files.
// meta must have StartedAt, SessionID and Device.ID set. The directory must
// not already exist.
func Create(dataDir string, meta Metadata, raw bool) (*Session, error) {
	if err := os.MkdirAll(dataDir, 0o755); err != nil {
		return nil, fmt.Errorf("create data directory: %w", err)
	}
	dir := filepath.Join(dataDir, DirName(meta.StartedAt, meta.Device.ID, meta.SessionID))
	if err := os.Mkdir(dir, 0o755); err != nil {
		return nil, fmt.Errorf("create session directory: %w", err)
	}
	s := &Session{Dir: dir, Meta: meta, start: meta.StartedAt.UTC()}
	s.Meta.SchemaVersion = SchemaVersion
	s.Meta.Status = StatusRecording
	s.Meta.Recording.Raw = raw

	var err error
	open := func(name, header string) *file {
		if err != nil {
			return nil
		}
		var f *file
		f, err = createFile(filepath.Join(dir, name), header)
		return f
	}
	s.hr = open("hr.csv", HRHeader)
	s.rr = open("rr.csv", RRHeader)
	s.ecg = open("ecg.csv", ECGHeader)
	s.events = open("events.jsonl", "")
	if raw && err == nil {
		if err = os.Mkdir(filepath.Join(dir, "raw"), 0o755); err == nil {
			s.raw = open(filepath.Join("raw", "ble.jsonl"), "")
		}
	}
	if err == nil {
		err = WriteMetadata(dir, &s.Meta)
	}
	if err != nil {
		s.closeFiles()
		return nil, fmt.Errorf("create session %s: %w", dir, err)
	}
	return s, nil
}

func createFile(path, header string) (*file, error) {
	// O_EXCL: never overwrite existing data.
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o644)
	if err != nil {
		return nil, err
	}
	w := bufio.NewWriterSize(f, 64<<10)
	if header != "" {
		w.WriteString(header + "\n")
	}
	return &file{f: f, w: w}, nil
}

// Err returns the first write error, if any.
func (s *Session) Err() error { return s.err }

// Time converts elapsed ns since session start to the stored wall time.
func (s *Session) Time(elapsed int64) time.Time {
	return s.start.Add(time.Duration(elapsed))
}

func (s *Session) write(f *file, b []byte) error {
	if s.err != nil {
		return s.err
	}
	if s.closed {
		s.err = errors.New("session is closed")
		return s.err
	}
	if _, err := f.w.Write(b); err != nil {
		s.err = fmt.Errorf("write %s: %w", f.f.Name(), err)
	}
	return s.err
}

func (s *Session) appendTime(b []byte, elapsed int64) []byte {
	b = s.Time(elapsed).AppendFormat(b, TimeFormat)
	b = append(b, ',')
	return strconv.AppendInt(b, elapsed, 10)
}

// WriteHR appends a heart rate row.
func (s *Session) WriteHR(elapsed int64, bpm int) error {
	b := s.appendTime(make([]byte, 0, 64), elapsed)
	b = append(b, ',')
	b = strconv.AppendInt(b, int64(bpm), 10)
	return s.write(s.hr, append(b, '\n'))
}

// WriteRR appends an RR interval row. rrMs is written with the shortest
// exact decimal representation (no rounding).
func (s *Session) WriteRR(elapsed int64, rrMs float64) error {
	b := s.appendTime(make([]byte, 0, 64), elapsed)
	b = append(b, ',')
	b = strconv.AppendFloat(b, rrMs, 'f', -1, 64)
	return s.write(s.rr, append(b, '\n'))
}

// WriteECG appends an ECG sample row.
func (s *Session) WriteECG(elapsed, index int64, uv int32) error {
	b := s.appendTime(make([]byte, 0, 72), elapsed)
	b = append(b, ',')
	b = strconv.AppendInt(b, index, 10)
	b = append(b, ',')
	b = strconv.AppendInt(b, int64(uv), 10)
	return s.write(s.ecg, append(b, '\n'))
}

// WriteEvent appends an event line: timestamp, elapsed_ns and type first,
// then fields in sorted key order.
func (s *Session) WriteEvent(elapsed int64, typ string, fields map[string]any) error {
	b, err := EncodeEvent(s.Time(elapsed), elapsed, typ, fields)
	if err != nil {
		return err
	}
	return s.write(s.events, b)
}

// EncodeEvent renders one events.jsonl line.
func EncodeEvent(ts time.Time, elapsed int64, typ string, fields map[string]any) ([]byte, error) {
	b := []byte(`{"timestamp":"`)
	b = ts.UTC().AppendFormat(b, TimeFormat)
	b = append(b, `","elapsed_ns":`...)
	b = strconv.AppendInt(b, elapsed, 10)
	b = append(b, `,"type":`...)
	b = strconv.AppendQuote(b, typ)
	keys := make([]string, 0, len(fields))
	for k := range fields {
		if k != "timestamp" && k != "elapsed_ns" && k != "type" {
			keys = append(keys, k)
		}
	}
	sort.Strings(keys)
	for _, k := range keys {
		v, err := json.Marshal(fields[k])
		if err != nil {
			return nil, fmt.Errorf("encode event field %s: %w", k, err)
		}
		b = append(b, ',')
		b = strconv.AppendQuote(b, k)
		b = append(b, ':')
		b = append(b, v...)
	}
	return append(b, '}', '\n'), nil
}

// RawEnabled reports whether raw BLE capture is on.
func (s *Session) RawEnabled() bool { return s.raw != nil }

// WriteRaw appends a raw BLE frame to raw/ble.jsonl.
func (s *Session) WriteRaw(received time.Time, elapsed int64, char, name string, tx bool, data []byte) error {
	if s.raw == nil {
		return nil
	}
	b := []byte(`{"received_at":"`)
	b = received.UTC().AppendFormat(b, TimeFormat)
	b = append(b, `","elapsed_ns":`...)
	b = strconv.AppendInt(b, elapsed, 10)
	b = append(b, `,"direction":"`...)
	if tx {
		b = append(b, "tx"...)
	} else {
		b = append(b, "rx"...)
	}
	b = append(b, `","characteristic":"`...)
	b = append(b, char...)
	b = append(b, `","name":"`...)
	b = append(b, name...)
	b = append(b, `","data":"`...)
	b = hex.AppendEncode(b, data)
	b = append(b, "\"}\n"...)
	return s.write(s.raw, b)
}

func (s *Session) files() []*file {
	var fs []*file
	for _, f := range []*file{s.hr, s.rr, s.ecg, s.events, s.raw} {
		if f != nil {
			fs = append(fs, f)
		}
	}
	return fs
}

// Flush writes buffered data to the operating system.
func (s *Session) Flush() error {
	if s.err != nil {
		return s.err
	}
	for _, f := range s.files() {
		if err := f.w.Flush(); err != nil {
			s.err = fmt.Errorf("write %s: %w", f.f.Name(), err)
			return s.err
		}
	}
	return nil
}

// Checkpoint flushes all files and atomically rewrites metadata.json with the
// current (still "recording") state so a crash leaves recent statistics.
func (s *Session) Checkpoint() error {
	if err := s.Flush(); err != nil {
		return err
	}
	if err := WriteMetadata(s.Dir, &s.Meta); err != nil {
		s.err = err
	}
	return s.err
}

func (s *Session) closeFiles() error {
	var first error
	for _, f := range s.files() {
		if err := f.w.Flush(); err != nil && first == nil {
			first = fmt.Errorf("write %s: %w", f.f.Name(), err)
		}
		if err := f.f.Sync(); err != nil && first == nil {
			first = fmt.Errorf("sync %s: %w", f.f.Name(), err)
		}
		if err := f.f.Close(); err != nil && first == nil {
			first = fmt.Errorf("close %s: %w", f.f.Name(), err)
		}
	}
	return first
}

// Close flushes, syncs and closes all files, writes the final metadata
// (s.Meta, with end time = start + elapsed) and marks the session files
// read-only. If a write error occurred, the session is marked failed.
// Close is idempotent.
func (s *Session) Close(elapsed int64) error {
	if s.closed {
		return s.err
	}
	s.closed = true
	if err := s.closeFiles(); err != nil && s.err == nil {
		s.err = err
	}

	end := s.Time(elapsed)
	dur := end.Sub(s.start).Seconds()
	s.Meta.EndedAt = &end
	s.Meta.DurationSeconds = &dur
	if s.err != nil {
		s.Meta.Status = StatusFailed
		if s.Meta.Error == "" {
			s.Meta.Error = s.err.Error()
		}
	} else if s.Meta.Status == StatusRecording {
		s.Meta.Status = StatusCompleted
	}
	if err := WriteMetadata(s.Dir, &s.Meta); err != nil {
		if s.err == nil {
			s.err = err
		}
		return s.err
	}
	// Recorded data is immutable: drop write permission.
	filepath.WalkDir(s.Dir, func(p string, d fs.DirEntry, err error) error {
		if err == nil && !d.IsDir() {
			os.Chmod(p, 0o444)
		}
		return nil
	})
	return s.err
}
