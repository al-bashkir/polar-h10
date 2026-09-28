package polar

import (
	"bufio"
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"
)

// TestFixtureFrames decodes synthetic frames stored in the raw/ble.jsonl
// format, laid out exactly like H10 frames (73-sample ECG frame, 229 bytes).
// The values are made up; no recorded data is used.
func TestFixtureFrames(t *testing.T) {
	f, err := os.Open("testdata/synthetic_frames.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	defer f.Close()
	frames := map[string][]byte{}
	sc := bufio.NewScanner(f)
	sc.Buffer(nil, 1<<20)
	for sc.Scan() {
		var line struct{ Name, Data string }
		if err := json.Unmarshal(sc.Bytes(), &line); err != nil {
			t.Fatal(err)
		}
		b, err := hex.DecodeString(line.Data)
		if err != nil {
			t.Fatal(err)
		}
		frames[line.Name] = b
	}

	r, err := ParseCPResponse(frames["pmd_control"])
	if err != nil || r.Op != 0x02 || r.Meas != MeasECG || r.Err() != nil {
		t.Errorf("start response = %+v, %v", r, err)
	}

	hr, err := ParseHR(frames["hr_measurement"])
	if err != nil || hr.HeartRate != 60 || len(hr.RR) != 1 || RRMillis(hr.RR[0]) != 1000 || !hr.ContactDetected {
		t.Errorf("hr = %+v, %v", hr, err)
	}

	ecg, err := ParseECGFrame(frames["pmd_data"])
	if err != nil {
		t.Fatal(err)
	}
	if len(ecg.Samples) != 73 {
		t.Errorf("samples = %d, want 73", len(ecg.Samples))
	}
	if ecg.DeviceTimestamp != 0x082386f26fc10000 {
		t.Errorf("timestamp = %#x", ecg.DeviceTimestamp)
	}
	for i, want := range map[int]int32{0: -80, 9: -44, 35: 600, 36: 1500, 40: -350, 72: -72} {
		if ecg.Samples[i] != want {
			t.Errorf("sample %d = %d, want %d", i, ecg.Samples[i], want)
		}
	}
}
