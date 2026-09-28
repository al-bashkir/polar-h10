package cli

import (
	"context"
	"flag"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"text/tabwriter"
	"time"

	"github.com/al-bashkir/polar-h10/internal/ble"
	"github.com/al-bashkir/polar-h10/internal/polar"
	"github.com/al-bashkir/polar-h10/internal/recorder"
	"github.com/al-bashkir/polar-h10/internal/storage"
)

func (a *app) scan(args []string) error {
	fs := flag.NewFlagSet("scan", flag.ContinueOnError)
	timeout := fs.Duration("timeout", 10*time.Second, "how long to scan")
	all := fs.Bool("all", false, "list all BLE devices, not only Polar H10")
	if err := parseFlags(fs, args); err != nil {
		return err
	}
	ctx, stop := a.signalContext()
	defer stop()
	ad, err := ble.Open()
	if err != nil {
		return err
	}
	fmt.Fprintf(os.Stderr, "Scanning for %s...\n", *timeout)

	var mu sync.Mutex
	seen := map[string]ble.Advertisement{}
	sctx, cancel := context.WithTimeout(ctx, *timeout)
	defer cancel()
	err = ad.Scan(sctx, func(adv ble.Advertisement) bool {
		if !*all && !polar.IsH10(adv.Name) {
			return true
		}
		mu.Lock()
		defer mu.Unlock()
		if prev, ok := seen[adv.Address]; !ok || adv.RSSI > prev.RSSI || prev.Name == "" {
			seen[adv.Address] = adv
		}
		return true
	})
	if err != nil {
		return err
	}
	mu.Lock()
	defer mu.Unlock()
	if len(seen) == 0 {
		return fmt.Errorf("%w within %s\nhint: wear the strap with moistened electrodes and disconnect it from other apps",
			errNotFound, *timeout)
	}
	list := make([]ble.Advertisement, 0, len(seen))
	for _, adv := range seen {
		list = append(list, adv)
	}
	sort.Slice(list, func(i, j int) bool { return list[i].RSSI > list[j].RSSI })

	tw := tabwriter.NewWriter(a.stdout, 0, 0, 3, ' ', 0)
	fmt.Fprintln(tw, "DEVICE\tID\tRSSI\tADDRESS")
	for _, adv := range list {
		name, id := adv.Name, polar.DeviceID(adv.Name)
		if polar.IsH10(name) {
			name = polar.NamePrefix
		}
		fmt.Fprintf(tw, "%s\t%s\t%d\t%s\n", orDash(name), orDash(id), adv.RSSI, adv.Address)
	}
	return tw.Flush()
}

func orDash(s string) string {
	if s == "" {
		return "-"
	}
	return s
}

func (a *app) info(args []string) error {
	fs := flag.NewFlagSet("info", flag.ContinueOnError)
	device := fs.String("device", "", "Polar device ID (default: preferred_device or first found)")
	if err := parseFlags(fs, args); err != nil {
		return err
	}
	ctx, stop := a.signalContext()
	defer stop()
	c, err := a.connect(ctx, a.target(*device), nil)
	if err != nil {
		return err
	}
	defer c.dev.Close()
	i := c.dev.Info

	measurements := []string{}
	if i.HasHR {
		measurements = append(measurements, "hr", "rr")
	}
	if i.Features[polar.MeasECG] {
		measurements = append(measurements, "ecg")
	}
	ecg := "not supported"
	if i.Features[polar.MeasECG] {
		if s, err := c.dev.ECGSettings(); err != nil {
			ecg = "error: " + err.Error()
		} else {
			ecg = fmt.Sprintf("sample rate %v Hz, resolution %v bit",
				joinUints(s[polar.SettingSampleRate]), joinUints(s[polar.SettingResolution]))
		}
	}
	battery := "unknown"
	if i.Battery >= 0 {
		battery = fmt.Sprintf("%d %%", i.Battery)
	}
	state := "disconnected"
	if c.dev.Connected() {
		state = "connected"
	}
	tw := tabwriter.NewWriter(a.stdout, 0, 0, 2, ' ', 0)
	for _, kv := range [][2]string{
		{"Name", i.Name},
		{"ID", i.ID},
		{"Address", i.Address},
		{"Connection", state},
		{"Battery", battery},
		{"Manufacturer", i.Manufacturer},
		{"Model", i.Model},
		{"Serial", i.Serial},
		{"Hardware", i.Hardware},
		{"Firmware", i.Firmware},
		{"Software", i.Software},
		{"Measurements", strings.Join(measurements, ", ")},
		{"PMD features", i.Features.String()},
		{"ECG settings", ecg},
	} {
		fmt.Fprintf(tw, "%s\t%s\n", kv[0], orDash(kv[1]))
	}
	return tw.Flush()
}

func joinUints(v []uint32) string {
	s := make([]string, len(v))
	for i, x := range v {
		s[i] = fmt.Sprint(x)
	}
	return strings.Join(s, "/")
}

func (a *app) monitor(args []string) error {
	fs := flag.NewFlagSet("monitor", flag.ContinueOnError)
	device := fs.String("device", "", "Polar device ID (default: preferred_device or first found)")
	noECG := fs.Bool("no-ecg", false, "do not stream ECG")
	if err := parseFlags(fs, args); err != nil {
		return err
	}
	ctx, stop := a.signalContext()
	defer stop()

	start := time.Now()
	rec := recorder.New(start, nil, 0, a.log)
	defer rec.Stop()
	c, err := a.connect(ctx, a.target(*device), rec.Packet)
	if err != nil {
		return err
	}
	if !*noECG && !c.dev.Info.Features[polar.MeasECG] {
		c.dev.Close()
		return fmt.Errorf("ecg: %w (use --no-ecg)", polar.ErrNotSupported)
	}
	rec.Event(recorder.EvDeviceConnected, map[string]any{"name": c.dev.Info.Name})
	if c.dev.Info.Battery >= 0 {
		rec.Event(recorder.EvBatteryLevel, map[string]any{"percent": c.dev.Info.Battery})
	}

	opt := a.acquireOptions(!*noECG, nil)
	err = a.runWithDisplay(ctx, c, rec, opt, func() ([]string, string) {
		return statusBlock(c.dev.Info.Name, rec.Status(), start, opt.ECG, "", "Ctrl+C to stop")
	})
	a.disp.done() // leave the last state on screen
	return err
}

func (a *app) acquireOptions(ecg bool, reconnect *int) recorder.AcquireOptions {
	opt := recorder.DefaultAcquireOptions()
	opt.ECG = ecg
	if a.cfg.ReconnectAttempts != nil {
		opt.ReconnectAttempts = *a.cfg.ReconnectAttempts
	}
	if reconnect != nil && *reconnect >= 0 {
		opt.ReconnectAttempts = *reconnect
	}
	return opt
}

// runWithDisplay runs acquisition while refreshing the status display.
func (a *app) runWithDisplay(ctx context.Context, c connection, rec *recorder.Recorder,
	opt recorder.AcquireOptions, render func() ([]string, string)) error {
	done := make(chan error, 1)
	go func() {
		done <- recorder.Acquire(ctx, c.dev, rec, a.dialer(c), opt)
	}()
	tick := time.NewTicker(500 * time.Millisecond)
	defer tick.Stop()
	for {
		a.disp.show(render())
		select {
		case err := <-done:
			return err
		case <-tick.C:
		}
	}
}

func (a *app) sessions(args []string) error {
	fs := flag.NewFlagSet("sessions", flag.ContinueOnError)
	output := fs.String("output", "", "data directory (default: data_dir or ~/h10-data)")
	if err := parseFlags(fs, args); err != nil {
		return err
	}
	dir := a.cfg.dataDir(*output)
	list, err := storage.ListSessions(dir)
	if err != nil {
		return err
	}
	if len(list) == 0 {
		fmt.Fprintf(os.Stderr, "No sessions in %s\n", dir)
		return nil
	}
	tw := tabwriter.NewWriter(a.stdout, 0, 0, 3, ' ', 0)
	fmt.Fprintln(tw, "DATE\tDURATION\tDEVICE\tNAME\tSTATUS\tDIRECTORY")
	for _, e := range list {
		base := filepath.Base(e.Dir)
		if e.Meta == nil {
			fmt.Fprintf(tw, "?\t?\t?\t?\tunreadable\t%s\n", base)
			continue
		}
		m := e.Meta
		dur := "-"
		if m.DurationSeconds != nil {
			dur = (time.Duration(*m.DurationSeconds * float64(time.Second))).Round(time.Second).String()
		}
		status := m.Status
		if status == storage.StatusRecording {
			status = "incomplete" // never finalized: still running or crashed
		}
		fmt.Fprintf(tw, "%s\t%s\t%s\t%s\t%s\t%s\n", m.StartedAt.Local().Format("2006-01-02 15:04"), dur,
			orDash(m.Device.ID), orDash(m.Name), status, base)
	}
	return tw.Flush()
}
