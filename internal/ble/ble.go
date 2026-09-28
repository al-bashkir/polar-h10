// Package ble is a thin wrapper around tinygo.org/x/bluetooth (CoreBluetooth
// on macOS, BlueZ over D-Bus on Linux). It exposes scanning and a connected
// GATT client that implements polar.Link.
package ble

import (
	"context"
	"errors"
	"fmt"
	"runtime"
	"strings"
	"sync"
	"time"

	"tinygo.org/x/bluetooth"
)

// Adapter is the host Bluetooth adapter.
type Adapter struct {
	bt *bluetooth.Adapter
	mu sync.Mutex // one scan or connect at a time
}

var (
	enableOnce sync.Once
	enableErr  error
)

// Open enables the default adapter.
func Open() (*Adapter, error) {
	a := bluetooth.DefaultAdapter
	enableOnce.Do(func() { enableErr = a.Enable() })
	if enableErr != nil {
		return nil, explain(fmt.Errorf("enable bluetooth adapter: %w", enableErr))
	}
	return &Adapter{bt: a}, nil
}

// explain adds platform-specific hints to common failures.
func explain(err error) error {
	msg := err.Error()
	switch {
	case strings.Contains(msg, "not authorized"):
		return fmt.Errorf("%w\nhint: allow Bluetooth access for your terminal app in "+
			"System Settings > Privacy & Security > Bluetooth, then restart the terminal", err)
	case strings.Contains(msg, "powered off"), strings.Contains(msg, "not powered"):
		return fmt.Errorf("%w\nhint: turn Bluetooth on", err)
	case runtime.GOOS == "linux" && (strings.Contains(msg, "AccessDenied") || strings.Contains(msg, "NotPermitted")):
		return fmt.Errorf("%w\nhint: your user needs permission to use BlueZ over D-Bus "+
			"(e.g. membership in the 'bluetooth' group)", err)
	case runtime.GOOS == "linux" && (strings.Contains(msg, "ServiceUnknown") || strings.Contains(msg, "org.bluez")):
		return fmt.Errorf("%w\nhint: make sure bluetoothd is running (systemctl status bluetooth) "+
			"and an adapter is present (bluetoothctl list)", err)
	}
	return err
}

// Advertisement is a discovered peripheral.
type Advertisement struct {
	Name    string
	Address string
	RSSI    int
	addr    bluetooth.Address
}

// Scan reports advertisements until ctx is done or fn returns false. The same
// device may be reported more than once.
func (a *Adapter) Scan(ctx context.Context, fn func(Advertisement) bool) error {
	a.mu.Lock()
	defer a.mu.Unlock()

	var stopMu sync.Mutex
	stopped := false
	stop := func() {
		stopMu.Lock()
		defer stopMu.Unlock()
		if !stopped {
			stopped = true
			a.bt.StopScan()
		}
	}
	scanDone := make(chan struct{})
	defer close(scanDone)
	go func() {
		select {
		case <-ctx.Done():
			stop()
		case <-scanDone:
		}
	}()
	err := a.bt.Scan(func(_ *bluetooth.Adapter, r bluetooth.ScanResult) {
		adv := Advertisement{Name: r.LocalName(), Address: r.Address.String(), RSSI: int(r.RSSI), addr: r.Address}
		if !fn(adv) {
			stop()
		}
	})
	if err != nil {
		return explain(fmt.Errorf("scan: %w", err))
	}
	return nil
}

// Connect connects to an advertised device and discovers its GATT database.
func (a *Adapter) Connect(ctx context.Context, adv Advertisement, timeout time.Duration) (*Conn, error) {
	a.mu.Lock()
	defer a.mu.Unlock()

	ch := make(chan connResult, 1)
	go func() {
		dev, err := a.bt.Connect(adv.addr, bluetooth.ConnectionParams{ConnectionTimeout: bluetooth.NewDuration(timeout)})
		ch <- connResult{dev, err}
	}()
	var r connResult
	select {
	case r = <-ch:
	case <-ctx.Done():
		go abandon(ch)
		return nil, context.Cause(ctx)
	case <-time.After(timeout + 5*time.Second): // BlueZ Connect has no timeout of its own
		go abandon(ch)
		return nil, fmt.Errorf("connect to %s: timed out after %s", adv.Name, timeout)
	}
	if r.err != nil {
		return nil, explain(fmt.Errorf("connect to %s: %w", adv.Name, r.err))
	}

	c := &Conn{dev: r.dev, chars: map[string]*bluetooth.DeviceCharacteristic{}}
	if err := c.discover(); err != nil {
		r.dev.Disconnect()
		return nil, fmt.Errorf("discover services on %s: %w", adv.Name, err)
	}
	return c, nil
}

type connResult struct {
	dev bluetooth.Device
	err error
}

// abandon disconnects a connection that completes after its caller gave up.
func abandon(ch <-chan connResult) {
	if r := <-ch; r.err == nil {
		r.dev.Disconnect()
	}
}

// Conn is a connected GATT client. It implements polar.Link.
type Conn struct {
	dev   bluetooth.Device
	chars map[string]*bluetooth.DeviceCharacteristic
	mu    sync.Mutex // serializes GATT operations
}

func (c *Conn) discover() error {
	svcs, err := c.dev.DiscoverServices(nil)
	if err != nil {
		return err
	}
	for _, s := range svcs {
		chars, err := s.DiscoverCharacteristics(nil)
		if err != nil {
			return err
		}
		for i := range chars {
			ch := chars[i]
			c.chars[strings.ToLower(ch.UUID().String())] = &ch
		}
	}
	return nil
}

var errNoChar = errors.New("characteristic not found")

func (c *Conn) char(uuid string) (*bluetooth.DeviceCharacteristic, error) {
	ch, ok := c.chars[uuid]
	if !ok {
		return nil, fmt.Errorf("%s: %w", uuid, errNoChar)
	}
	return ch, nil
}

// Has reports whether the device exposes a characteristic.
func (c *Conn) Has(uuid string) bool { _, ok := c.chars[uuid]; return ok }

// Read reads a characteristic value.
func (c *Conn) Read(uuid string) ([]byte, error) {
	ch, err := c.char(uuid)
	if err != nil {
		return nil, err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	buf := make([]byte, 512)
	n, err := ch.Read(buf)
	if err != nil {
		return nil, fmt.Errorf("read %s: %w", uuid, err)
	}
	return buf[:min(n, len(buf))], nil
}

// Write writes a characteristic value with response.
func (c *Conn) Write(uuid string, data []byte) error {
	ch, err := c.char(uuid)
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if _, err := ch.Write(data); err != nil {
		return fmt.Errorf("write %s: %w", uuid, err)
	}
	return nil
}

// Subscribe enables notifications/indications on a characteristic.
func (c *Conn) Subscribe(uuid string, fn func([]byte)) error {
	ch, err := c.char(uuid)
	if err != nil {
		return err
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	if err := ch.EnableNotifications(fn); err != nil {
		return fmt.Errorf("enable notifications on %s: %w", uuid, err)
	}
	return nil
}

// Connected reports whether the link is up.
func (c *Conn) Connected() bool {
	ok, err := c.dev.Connected()
	return err == nil && ok
}

// Close disconnects.
func (c *Conn) Close() error { return c.dev.Disconnect() }
