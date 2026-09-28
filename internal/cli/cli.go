// Package cli implements the h10 command line interface.
package cli

import (
	"context"
	"errors"
	"flag"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/al-bashkir/polar-h10/internal/ble"
	"github.com/al-bashkir/polar-h10/internal/polar"
	"github.com/al-bashkir/polar-h10/internal/recorder"
)

// Version is the application version; override with -ldflags "-X ...".
var Version = "0.1.0"

const usage = `h10 - Polar H10 heart rate, RR and ECG recorder

Usage:
  h10 [global flags] <command> [flags]

Commands:
  scan       discover Polar H10 devices
  info       show device information and capabilities
  monitor    display live measurements without recording
  record     record a session to the data directory
  sessions   list recorded sessions
  version    print version

Global flags:
  --verbose           log informational messages (to stderr)
  --log-level LEVEL   debug, info, warn (default) or error
  --config PATH       config file (default: %s)

Run "h10 <command> -h" for command flags.
`

type app struct {
	cfg     Config
	log     *slog.Logger
	level   *slog.LevelVar
	disp    *display
	stdout  io.Writer
	verbose bool
}

// Run executes the CLI and returns the process exit code.
func Run(args []string) int {
	defCfg := defaultConfigPath()
	g := flag.NewFlagSet("h10", flag.ContinueOnError)
	g.Usage = func() { fmt.Fprintf(os.Stderr, usage, defCfg) }
	verbose := g.Bool("verbose", false, "")
	levelName := g.String("log-level", "", "")
	cfgPath := g.String("config", "", "")
	if err := g.Parse(args); err != nil {
		return 2
	}
	if g.NArg() == 0 {
		g.Usage()
		return 2
	}

	level := new(slog.LevelVar)
	level.Set(slog.LevelWarn)
	if *verbose {
		level.Set(slog.LevelInfo)
	}
	if *levelName != "" {
		if err := level.UnmarshalText([]byte(*levelName)); err != nil {
			fmt.Fprintf(os.Stderr, "h10: invalid --log-level %q\n", *levelName)
			return 2
		}
	}
	a := &app{level: level, stdout: os.Stdout, verbose: level.Level() < slog.LevelWarn}
	a.disp = newDisplay(os.Stdout, !a.verbose && isTerminal(os.Stdout))
	// Logs go through the display so they never tear the live status block.
	a.log = slog.New(slog.NewTextHandler(a.disp.logWriter(os.Stderr), &slog.HandlerOptions{Level: level}))

	cfg, err := loadConfig(*cfgPath, *cfgPath != "")
	if err != nil {
		fmt.Fprintf(os.Stderr, "h10: %v\n", err)
		return 1
	}
	a.cfg = cfg

	cmd, rest := g.Arg(0), g.Args()[1:]
	var run func([]string) error
	switch cmd {
	case "scan":
		run = a.scan
	case "info":
		run = a.info
	case "monitor":
		run = a.monitor
	case "record":
		run = a.record
	case "sessions":
		run = a.sessions
	case "version":
		run = func([]string) error { fmt.Println("h10", Version); return nil }
	case "help":
		g.Usage()
		return 0
	default:
		fmt.Fprintf(os.Stderr, "h10: unknown command %q\n\n", cmd)
		g.Usage()
		return 2
	}
	if err := run(rest); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return 0
		}
		var ue usageError
		if errors.As(err, &ue) {
			fmt.Fprintf(os.Stderr, "h10 %s: %v\n", cmd, err)
			return 2
		}
		fmt.Fprintf(os.Stderr, "h10 %s: %v\n", cmd, err)
		return 1
	}
	return 0
}

type usageError struct{ error }

func parseFlags(fs *flag.FlagSet, args []string) error {
	if err := fs.Parse(args); err != nil {
		if errors.Is(err, flag.ErrHelp) {
			return err
		}
		return usageError{err}
	}
	if fs.NArg() > 0 {
		return usageError{fmt.Errorf("unexpected arguments: %s", strings.Join(fs.Args(), " "))}
	}
	return nil
}

// Stop causes, used to derive the session stop reason.
var (
	errInterrupted = errors.New("interrupted")
	errTerminated  = errors.New("terminated")
	errDuration    = errors.New("requested duration reached")
)

// signalContext is cancelled on the first SIGINT/SIGTERM with a cause. A
// second signal exits immediately.
func (a *app) signalContext() (context.Context, func()) {
	ctx, cancel := context.WithCancelCause(context.Background())
	ch := make(chan os.Signal, 2)
	signal.Notify(ch, os.Interrupt, syscall.SIGTERM)
	go func() {
		sig, ok := <-ch
		if !ok {
			return
		}
		if sig == syscall.SIGTERM {
			cancel(errTerminated)
		} else {
			cancel(errInterrupted)
		}
		if _, ok := <-ch; ok {
			a.disp.clear()
			fmt.Fprintln(os.Stderr, "h10: forced exit; files are flushed every second but metadata.json was not finalized")
			os.Exit(130)
		}
	}()
	return ctx, func() { signal.Stop(ch); close(ch); cancel(nil) }
}

// target describes which device to use.
type target struct {
	id      string // Polar device ID; "" = first H10 found
	timeout time.Duration
}

func (a *app) target(flagID string) target {
	id := flagID
	if id == "" {
		id = a.cfg.PreferredDevice
	}
	return target{id: strings.ToUpper(id), timeout: 20 * time.Second}
}

var errNotFound = errors.New("polar H10 not found")

// find scans until a matching H10 is seen.
func (a *app) find(ctx context.Context, ad *ble.Adapter, t target) (ble.Advertisement, error) {
	ctx, cancel := context.WithTimeout(ctx, t.timeout)
	defer cancel()
	var mu sync.Mutex // callbacks may still run briefly after the scan stops
	var found *ble.Advertisement
	err := ad.Scan(ctx, func(adv ble.Advertisement) bool {
		mu.Lock()
		defer mu.Unlock()
		if found != nil {
			return false
		}
		if !polar.IsH10(adv.Name) || t.id != "" && !strings.EqualFold(polar.DeviceID(adv.Name), t.id) {
			return true
		}
		found = &adv
		return false
	})
	if err != nil {
		return ble.Advertisement{}, err
	}
	mu.Lock()
	defer mu.Unlock()
	if found == nil {
		if ctx.Err() != nil && context.Cause(ctx) != context.DeadlineExceeded {
			return ble.Advertisement{}, context.Cause(ctx)
		}
		what := "no Polar H10"
		if t.id != "" {
			what = "Polar H10 " + t.id
		}
		return ble.Advertisement{}, fmt.Errorf("%w: %s seen within %s\n"+
			"hint: wear the strap with moistened electrodes (the H10 sleeps otherwise), and make sure it is not "+
			"connected to another app or phone", errNotFound, what, t.timeout)
	}
	a.log.Info("found device", "name", found.Name, "address", found.Address, "rssi", found.RSSI)
	return *found, nil
}

// connection is an opened device plus what is needed to reconnect to it.
type connection struct {
	dev *polar.Device
	ad  *ble.Adapter
	adv ble.Advertisement
}

// connect finds and connects to the target and opens it as a Polar device.
func (a *app) connect(ctx context.Context, t target, sink func(polar.Packet)) (connection, error) {
	ad, err := ble.Open()
	if err != nil {
		return connection{}, err
	}
	adv, err := a.find(ctx, ad, t)
	if err != nil {
		return connection{}, err
	}
	dev, err := a.open(ctx, ad, adv, sink)
	return connection{dev: dev, ad: ad, adv: adv}, err
}

func (a *app) open(ctx context.Context, ad *ble.Adapter, adv ble.Advertisement, sink func(polar.Packet)) (*polar.Device, error) {
	conn, err := ad.Connect(ctx, adv, 15*time.Second)
	if err != nil {
		return nil, err
	}
	dev, err := polar.Open(conn, adv.Name, adv.Address, sink)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("open %s: %w", adv.Name, err)
	}
	return dev, nil
}

// dialer returns a reconnect function: first try the known address directly,
// then scan for the device ID again.
func (a *app) dialer(c connection) recorder.Dialer {
	known := c.adv
	return func(ctx context.Context, sink func(polar.Packet)) (*polar.Device, error) {
		dev, err := a.open(ctx, c.ad, known, sink)
		if err == nil {
			return dev, nil
		}
		a.log.Info("direct reconnect failed; scanning", "err", err)
		adv, err := a.find(ctx, c.ad, target{id: polar.DeviceID(known.Name), timeout: 20 * time.Second})
		if err != nil {
			return nil, err
		}
		known = adv
		return a.open(ctx, c.ad, adv, sink)
	}
}
