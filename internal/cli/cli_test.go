package cli

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/al-bashkir/polar-h10/internal/recorder"
)

func TestLoadConfig(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "config.json")
	os.WriteFile(p, []byte(`{"data_dir":"~/x","preferred_device":"abc","raw_recording":false,"reconnect_attempts":2}`), 0o644)
	c, err := loadConfig(p, true)
	if err != nil {
		t.Fatal(err)
	}
	home, _ := os.UserHomeDir()
	if c.dataDir("") != filepath.Join(home, "x") || c.PreferredDevice != "abc" || *c.RawRecording || *c.ReconnectAttempts != 2 {
		t.Errorf("config = %+v", c)
	}
	if c.dataDir("/flag") != "/flag" {
		t.Error("flag must override config")
	}

	os.WriteFile(p, []byte(`{"data_dri":"typo"}`), 0o644)
	if _, err := loadConfig(p, true); err == nil {
		t.Error("unknown field accepted")
	}
	if _, err := loadConfig(filepath.Join(dir, "missing.json"), true); err == nil {
		t.Error("missing explicit config accepted")
	}
	if c, err := loadConfig(filepath.Join(dir, "missing.json"), false); err != nil || c.DataDir != "" {
		t.Errorf("missing default config: %v %+v", err, c)
	}
}

func TestStopReason(t *testing.T) {
	ctx, cancel := context.WithCancelCause(context.Background())
	cancel(errInterrupted)
	cases := []struct {
		ctx  context.Context
		err  error
		want string
	}{
		{ctx, nil, "sigint"},
		{ctx, fmt.Errorf("x: %w", recorder.ErrStorage), "write_error"},
		{ctx, recorder.ErrDeviceLost, "device_lost"},
		{ctx, errors.New("boom"), "device_error"},
	}
	for _, c := range cases {
		if got := stopReason(c.ctx, c.err); got != c.want {
			t.Errorf("stopReason(%v) = %s, want %s", c.err, got, c.want)
		}
	}
	dctx, dcancel := context.WithTimeoutCause(context.Background(), 0, errDuration)
	defer dcancel()
	<-dctx.Done()
	if got := stopReason(dctx, nil); got != "duration" {
		t.Errorf("duration: %s", got)
	}
}

func TestFmtClock(t *testing.T) {
	if got := fmtClock(3*3600e9 + 2*60e9 + 31e9); got != "03:02:31" {
		t.Errorf("fmtClock = %s", got)
	}
}

func TestDisplayNonTTY(t *testing.T) {
	var b strings.Builder
	d := newDisplay(&b, false)
	d.show([]string{"a", "b"}, "summary")
	d.show([]string{"a", "b"}, "summary") // rate limited
	if b.String() != "summary\n" {
		t.Errorf("output = %q", b.String())
	}
}

func TestRunUsage(t *testing.T) {
	if code := Run([]string{"nope"}); code != 2 {
		t.Errorf("unknown command exit = %d", code)
	}
	if code := Run([]string{"sessions", "--output", t.TempDir()}); code != 0 {
		t.Errorf("sessions on empty dir exit = %d", code)
	}
	if code := Run([]string{"record", "--duration", "-1s"}); code != 2 {
		t.Errorf("negative duration exit = %d", code)
	}
}

func TestSparkline(t *testing.T) {
	// Flat baseline with two sharp spikes: spikes map to the top level.
	s := make([]int32, 96)
	for i := range s {
		s[i] = -100 + int32(i%3)
	}
	s[20], s[21] = 1400, -300
	s[70] = 1200
	got := []rune(sparkline(s, 12))
	if len(got) != 12 {
		t.Fatalf("width = %d", len(got))
	}
	if got[2] != '█' || got[8] < '▆' || got[0] != '▁' || got[5] != '▁' {
		t.Errorf("sparkline = %s", string(got))
	}
	if sparkline(nil, 10) != "" || len([]rune(sparkline(s[:5], 10))) != 5 {
		t.Error("edge cases")
	}
}
