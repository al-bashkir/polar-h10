package cli

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync/atomic"
	"time"

	"github.com/al-bashkir/polar-h10/internal/polar"
	"github.com/al-bashkir/polar-h10/internal/recorder"
	"github.com/al-bashkir/polar-h10/internal/storage"
)

func (a *app) record(args []string) error {
	fs := flag.NewFlagSet("record", flag.ContinueOnError)
	device := fs.String("device", "", "Polar device ID (default: preferred_device or first found)")
	output := fs.String("output", "", "data directory (default: data_dir or ~/h10-data)")
	name := fs.String("name", "", "session name stored in metadata")
	duration := fs.Duration("duration", 0, "stop after this long, e.g. 10m (default: until Ctrl+C)")
	noRaw := fs.Bool("no-raw", false, "do not store raw BLE frames in raw/ble.jsonl")
	noECG := fs.Bool("no-ecg", false, "record HR/RR only")
	reconnect := fs.Int("reconnect-attempts", -1, "reconnect attempts after a disconnect (default 5 or config)")
	if err := parseFlags(fs, args); err != nil {
		return err
	}
	if *duration < 0 {
		return usageError{errors.New("--duration must be positive")}
	}
	raw := !*noRaw
	if a.cfg.RawRecording != nil && !*noRaw {
		raw = *a.cfg.RawRecording
	}
	dataDir := a.cfg.dataDir(*output)
	opt := a.acquireOptions(!*noECG, reconnect)

	ctx, stop := a.signalContext()
	defer stop()

	// The recorder is created once the session exists; nothing streams
	// before then, but route packets through a pointer to be safe.
	var recPtr atomic.Pointer[recorder.Recorder]
	sink := func(p polar.Packet) {
		if r := recPtr.Load(); r != nil {
			r.Packet(p)
		}
	}
	fmt.Fprintln(os.Stderr, "Connecting...")
	c, err := a.connect(ctx, a.target(*device), sink)
	if err != nil {
		return err
	}
	info := c.dev.Info
	if opt.ECG && !info.Features[polar.MeasECG] {
		c.dev.Close()
		return fmt.Errorf("ecg: %w (use --no-ecg to record HR/RR only)", polar.ErrNotSupported)
	}

	start := time.Now()
	sess, err := storage.Create(dataDir, a.newMetadata(start, *name, info, opt, raw, *duration), raw)
	if err != nil {
		c.dev.Close()
		return err
	}
	rec := recorder.New(start, sess, 0, a.log)
	recPtr.Store(rec)
	rec.Event(recorder.EvSessionStarted, map[string]any{"name": *name, "session_id": sess.Meta.SessionID})
	rec.Event(recorder.EvDeviceConnected, map[string]any{
		"name": info.Name, "id": info.ID, "address": info.Address, "firmware": info.Firmware,
	})
	if info.Battery >= 0 {
		rec.Event(recorder.EvBatteryLevel, map[string]any{"percent": info.Battery})
	}

	actx := ctx
	if *duration > 0 {
		var cancel context.CancelFunc
		actx, cancel = context.WithTimeoutCause(ctx, *duration, errDuration)
		defer cancel()
	}
	title := info.Name + "  ● REC"
	acqErr := a.runWithDisplay(actx, c, rec, opt, func() ([]string, string) {
		st := rec.Status()
		extra := []string{
			fmt.Sprintf("%-12s%d HR, %d RR, %d ECG", "Samples", st.Stats.HRSamples, st.Stats.RRSamples, st.Stats.ECGSamples),
			fmt.Sprintf("%-12s%s", "Session", sess.Dir),
			"",
			"Ctrl+C to stop",
		}
		if *duration > 0 {
			extra[3] = fmt.Sprintf("Stops after %s (Ctrl+C to stop now)", *duration)
		}
		return statusBlock(title, st, start, opt.ECG, extra...)
	})
	a.disp.done()

	reason := stopReason(actx, acqErr)
	rec.Event(recorder.EvSessionStopped, map[string]any{"reason": reason})
	storeErr := rec.Stop()
	end := time.Now()

	st := rec.Status().Stats
	sess.Meta.Statistics = st
	sess.Meta.StopReason = reason
	if acqErr != nil {
		sess.Meta.Status = storage.StatusFailed
		sess.Meta.Error = acqErr.Error()
	}
	// Last known battery: final read on graceful stop, else latest reading.
	if b := rec.Status().Battery; b >= 0 {
		sess.Meta.Device.BatteryEndPercent = &b
	}
	closeErr := sess.Close(rec.Elapsed(end))

	fmt.Fprintf(a.stdout, "\nSession %s: %s, %s\n", sess.Meta.Status, reason, fmtClock(end.Sub(start)))
	fmt.Fprintf(a.stdout, "  %s\n", sess.Dir)
	fmt.Fprintf(a.stdout, "  %d HR, %d RR, %d ECG samples; %d packets, %d dropped, %d decode errors, %d ECG gaps, %d reconnections\n",
		st.HRSamples, st.RRSamples, st.ECGSamples, st.BLEPackets, st.DroppedPackets, st.DecodeErrors, st.ECGGaps, st.Reconnections)

	switch {
	case storeErr != nil:
		return fmt.Errorf("recording stopped: %w", storeErr)
	case closeErr != nil:
		return fmt.Errorf("finalize session: %w", closeErr)
	case acqErr != nil:
		return acqErr
	}
	return nil
}

func stopReason(ctx context.Context, acqErr error) string {
	switch {
	case errors.Is(acqErr, recorder.ErrStorage):
		return "write_error"
	case errors.Is(acqErr, recorder.ErrDeviceLost):
		return "device_lost"
	case acqErr != nil:
		return "device_error"
	}
	switch context.Cause(ctx) {
	case errDuration:
		return "duration"
	case errInterrupted:
		return "sigint"
	case errTerminated:
		return "sigterm"
	}
	return "stopped"
}

func (a *app) newMetadata(start time.Time, name string, info polar.Info, opt recorder.AcquireOptions,
	raw bool, duration time.Duration) storage.Metadata {
	id, err := storage.NewSessionID(start)
	if err != nil {
		id = fmt.Sprintf("%x", start.UnixNano()) // crypto/rand failure is practically impossible
	}
	host, _ := os.Hostname()
	var battery *int
	if info.Battery >= 0 {
		b := info.Battery
		battery = &b
	}
	m := storage.Metadata{
		SessionID: id,
		Name:      name,
		StartedAt: start.UTC(),
		Timezone:  localTimezone(),
		UTCOffset: start.Format("-07:00"),
		Device: storage.DeviceMeta{
			Name: info.Name, ID: info.ID, Address: info.Address,
			Manufacturer: info.Manufacturer, Model: info.Model, Serial: info.Serial,
			Hardware: info.Hardware, Firmware: info.Firmware, Software: info.Software,
			Features:            info.Features.String(),
			BatteryStartPercent: battery,
		},
		Recording: storage.RecordingMeta{
			HR: true, RR: true, ECG: opt.ECG,
			ReconnectAttempts:  opt.ReconnectAttempts,
			RequestedDurationS: int(duration.Seconds()),
		},
		Timing: storage.TimingMeta{
			TimestampBase: "timestamp = started_at + elapsed_ns; elapsed_ns from the host monotonic clock",
			HR:            "host receive time of the heart rate notification",
			RR:            "estimated time of the beat ending the interval (RR chain anchored to receive times)",
			ECG:           "sensor frame timestamps mapped to host time at the first frame; per-sample from sensor clock",
		},
		Application: storage.ApplicationMeta{Name: "h10", Version: Version, GoVersion: runtime.Version()},
		Host:        storage.HostMeta{OS: runtime.GOOS, Arch: runtime.GOARCH, Hostname: host},
	}
	if opt.ECG {
		m.Recording.ECGSampleRateHz = polar.ECGSampleRate
		m.Recording.ECGResolutionBits = polar.ECGResolution
	}
	return m
}

// localTimezone returns the IANA name of the local time zone if it can be
// determined, else Go's name for it.
func localTimezone() string {
	if tz := os.Getenv("TZ"); tz != "" {
		return strings.TrimPrefix(tz, ":")
	}
	if target, err := os.Readlink("/etc/localtime"); err == nil {
		if i := strings.Index(target, "zoneinfo/"); i >= 0 {
			return filepath.ToSlash(target[i+len("zoneinfo/"):])
		}
	}
	return time.Local.String()
}
