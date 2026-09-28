// Package storage implements the on-disk session format: one immutable
// directory per recording holding metadata.json, CSV sample files and JSONL
// event/raw logs. See docs/data-format.md.
package storage

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// SchemaVersion identifies the session file format. Increment it on any
// incompatible change to file layout, column meaning or units.
const SchemaVersion = 1

// Session status values stored in metadata.json.
const (
	StatusRecording = "recording" // written at start; stays if the process died
	StatusCompleted = "completed"
	StatusFailed    = "failed"
)

// Metadata is the content of metadata.json.
type Metadata struct {
	SchemaVersion   int        `json:"schema_version"`
	SessionID       string     `json:"session_id"`
	Name            string     `json:"name"`
	Status          string     `json:"status"`
	StopReason      string     `json:"stop_reason,omitempty"`
	Error           string     `json:"error,omitempty"`
	StartedAt       time.Time  `json:"started_at"`
	EndedAt         *time.Time `json:"ended_at"`
	DurationSeconds *float64   `json:"duration_seconds"`
	Timezone        string     `json:"timezone"`
	UTCOffset       string     `json:"utc_offset"`

	Device      DeviceMeta      `json:"device"`
	Recording   RecordingMeta   `json:"recording"`
	Timing      TimingMeta      `json:"timing"`
	Statistics  Statistics      `json:"statistics"`
	Application ApplicationMeta `json:"application"`
	Host        HostMeta        `json:"host"`
}

// DeviceMeta identifies the sensor.
type DeviceMeta struct {
	Name                string `json:"name"`
	ID                  string `json:"id"`
	Address             string `json:"address,omitempty"`
	Manufacturer        string `json:"manufacturer,omitempty"`
	Model               string `json:"model,omitempty"`
	Serial              string `json:"serial,omitempty"`
	Hardware            string `json:"hardware_revision,omitempty"`
	Firmware            string `json:"firmware,omitempty"`
	Software            string `json:"software_revision,omitempty"`
	Features            string `json:"pmd_features,omitempty"`
	BatteryStartPercent *int   `json:"battery_start_percent"`
	BatteryEndPercent   *int   `json:"battery_end_percent"`
}

// RecordingMeta describes acquisition settings and which files exist.
type RecordingMeta struct {
	HR                 bool `json:"hr"`
	RR                 bool `json:"rr"`
	ECG                bool `json:"ecg"`
	ECGSampleRateHz    int  `json:"ecg_sample_rate_hz"`
	ECGResolutionBits  int  `json:"ecg_resolution_bits"`
	Raw                bool `json:"raw"`
	ReconnectAttempts  int  `json:"reconnect_attempts"`
	RequestedDurationS int  `json:"requested_duration_seconds,omitempty"`
}

// TimingMeta documents how stored timestamps were derived.
type TimingMeta struct {
	// Every stored timestamp equals started_at + elapsed_ns; elapsed_ns comes
	// from the host monotonic clock (immune to wall clock adjustments).
	TimestampBase string `json:"timestamp_base"`
	HR            string `json:"hr_timestamp"`
	RR            string `json:"rr_timestamp"`
	ECG           string `json:"ecg_timestamp"`
}

// Statistics holds sample and error counters.
type Statistics struct {
	HRSamples            int64 `json:"hr_samples"`
	RRSamples            int64 `json:"rr_samples"`
	ECGSamples           int64 `json:"ecg_samples"`
	ECGFrames            int64 `json:"ecg_frames"`
	BLEPackets           int64 `json:"ble_packets"`
	DecodeErrors         int64 `json:"decode_errors"`
	DroppedPackets       int64 `json:"dropped_packets"`
	ECGGaps              int64 `json:"ecg_gaps"`
	ECGMissingSamplesEst int64 `json:"ecg_missing_samples_estimated"`
	ECGClockRebases      int64 `json:"ecg_clock_rebases"`
	RRChainRebases       int64 `json:"rr_chain_rebases"`
	Disconnects          int64 `json:"disconnects"`
	Reconnections        int64 `json:"reconnections"`
}

// ApplicationMeta identifies the recording software.
type ApplicationMeta struct {
	Name      string `json:"name"`
	Version   string `json:"version"`
	GoVersion string `json:"go_version"`
}

// HostMeta identifies the recording machine.
type HostMeta struct {
	OS       string `json:"os"`
	Arch     string `json:"arch"`
	Hostname string `json:"hostname,omitempty"`
}

// WriteMetadata atomically replaces dir/metadata.json: it writes a temporary
// file, syncs it, renames it over the target and syncs the directory.
func WriteMetadata(dir string, m *Metadata) error {
	b, err := json.MarshalIndent(m, "", "  ")
	if err != nil {
		return fmt.Errorf("encode metadata: %w", err)
	}
	b = append(b, '\n')

	tmp, err := os.CreateTemp(dir, ".metadata-*.json")
	if err != nil {
		return fmt.Errorf("write metadata: %w", err)
	}
	defer os.Remove(tmp.Name()) // no-op after a successful rename
	if _, err := tmp.Write(b); err != nil {
		tmp.Close()
		return fmt.Errorf("write metadata: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return fmt.Errorf("sync metadata: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close metadata: %w", err)
	}
	if err := os.Chmod(tmp.Name(), 0o644); err != nil {
		return fmt.Errorf("chmod metadata: %w", err)
	}
	if err := os.Rename(tmp.Name(), filepath.Join(dir, "metadata.json")); err != nil {
		return fmt.Errorf("replace metadata: %w", err)
	}
	return syncDir(dir)
}

// ReadMetadata loads dir/metadata.json.
func ReadMetadata(dir string) (*Metadata, error) {
	b, err := os.ReadFile(filepath.Join(dir, "metadata.json"))
	if err != nil {
		return nil, err
	}
	var m Metadata
	if err := json.Unmarshal(b, &m); err != nil {
		return nil, fmt.Errorf("%s: %w", filepath.Join(dir, "metadata.json"), err)
	}
	return &m, nil
}

func syncDir(dir string) error {
	d, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer d.Close()
	if err := d.Sync(); err != nil {
		return fmt.Errorf("sync %s: %w", dir, err)
	}
	return nil
}
