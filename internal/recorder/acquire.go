package recorder

import (
	"context"
	"errors"
	"fmt"
	"time"

	"github.com/al-bashkir/polar-h10/internal/polar"
)

// Dialer finds and connects to the target device, delivering its packets to
// sink. It is called for the initial connection and for every reconnect.
type Dialer func(ctx context.Context, sink func(polar.Packet)) (*polar.Device, error)

// AcquireOptions controls streaming and reconnection.
type AcquireOptions struct {
	ECG               bool
	ReconnectAttempts int           // after a disconnect; 0 disables reconnecting
	ReconnectDelay    time.Duration // base delay, doubled per attempt (max 30 s)
	PollInterval      time.Duration // connection state polling
	StallTimeout      time.Duration // no packets for this long = connection lost
	BatteryInterval   time.Duration
}

// DefaultAcquireOptions returns the options used by the CLI.
func DefaultAcquireOptions() AcquireOptions {
	return AcquireOptions{
		ECG:               true,
		ReconnectAttempts: 5,
		ReconnectDelay:    2 * time.Second,
		PollInterval:      time.Second,
		StallTimeout:      15 * time.Second,
		BatteryInterval:   5 * time.Minute,
	}
}

// ErrDeviceLost means the device disconnected and could not be reconnected.
var ErrDeviceLost = errors.New("device connection lost and reconnect attempts exhausted")

// ErrStorage means recording stopped because storage writes failed.
var ErrStorage = errors.New("storage write failed")

// Acquire streams from an already opened device until ctx is done (graceful
// stop: nil error), storage fails (ErrStorage), or the device is lost and
// cannot be reconnected (ErrDeviceLost). Streams are stopped, the final
// battery level is recorded as an event and the device is disconnected
// before returning.
func Acquire(ctx context.Context, dev *polar.Device, rec *Recorder, dial Dialer, opt AcquireOptions) error {
	if err := startStreams(dev, rec, opt); err != nil {
		dev.Close()
		return err
	}
	for {
		err := stream(ctx, dev, rec, opt)
		if err == nil || errors.Is(err, ErrStorage) {
			shutdown(dev, rec, opt)
			return err
		}
		rec.Event(EvConnectionLost, map[string]any{"error": err.Error()})
		dev.Close()
		dev, err = reconnect(ctx, rec, dial, opt)
		if err != nil {
			return err
		}
		if dev == nil { // ctx done while reconnecting
			return nil
		}
	}
}

func startStreams(dev *polar.Device, rec *Recorder, opt AcquireOptions) error {
	if err := dev.StartHR(); err != nil {
		return err
	}
	rec.Event(EvHRStreamStarted, nil)
	if opt.ECG {
		if err := dev.StartECG(); err != nil {
			return fmt.Errorf("start ecg: %w", err)
		}
		rec.Event(EvECGStreamStarted, map[string]any{
			"sample_rate_hz": polar.ECGSampleRate, "resolution_bits": polar.ECGResolution,
		})
	}
	return nil
}

// stream watches the connection. It returns nil when ctx is done, ErrStorage
// on storage failure, or an error describing why the connection was lost.
func stream(ctx context.Context, dev *polar.Device, rec *Recorder, opt AcquireOptions) error {
	poll := time.NewTicker(opt.PollInterval)
	defer poll.Stop()
	battery := time.NewTicker(opt.BatteryInterval)
	defer battery.Stop()
	since := time.Now()
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-rec.Failed():
			return ErrStorage
		case <-battery.C:
			if b, err := dev.ReadBattery(); err == nil {
				rec.Event(EvBatteryLevel, map[string]any{"percent": b})
			}
		case now := <-poll.C:
			if !dev.Connected() {
				return errors.New("device disconnected")
			}
			last := rec.Status().LastPacket
			if last.Before(since) {
				last = since
			}
			if opt.StallTimeout > 0 && now.Sub(last) > opt.StallTimeout {
				rec.Event(EvDataStalled, map[string]any{"silence_ms": now.Sub(last).Milliseconds()})
				return fmt.Errorf("no data received for %s", now.Sub(last).Round(time.Second))
			}
		}
	}
}

func reconnect(ctx context.Context, rec *Recorder, dial Dialer, opt AcquireOptions) (*polar.Device, error) {
	delay := opt.ReconnectDelay
	for attempt := 1; attempt <= opt.ReconnectAttempts; attempt++ {
		select {
		case <-ctx.Done():
			return nil, nil
		case <-time.After(delay):
		}
		delay = min(delay*2, 30*time.Second)

		rec.Event(EvReconnectAttempt, map[string]any{"attempt": attempt, "max_attempts": opt.ReconnectAttempts})
		dev, err := dial(ctx, rec.Packet)
		if err == nil {
			if err = startStreams(dev, rec, opt); err != nil {
				dev.Close()
			}
		}
		if err != nil {
			if ctx.Err() != nil {
				return nil, nil
			}
			rec.Event(EvReconnectFailed, map[string]any{"attempt": attempt, "error": err.Error()})
			continue
		}
		rec.Event(EvDeviceReconnected, map[string]any{"attempt": attempt})
		return dev, nil
	}
	return nil, ErrDeviceLost
}

// shutdown stops streaming, reads the final battery level and disconnects.
// BLE calls can hang on a dying link, so the whole sequence is bounded.
func shutdown(dev *polar.Device, rec *Recorder, opt AcquireOptions) {
	done := make(chan struct{})
	go func() {
		defer close(done)
		if !dev.Connected() {
			return
		}
		if opt.ECG {
			if err := dev.StopECG(); err == nil {
				rec.Event(EvECGStreamStopped, nil)
			}
		}
		if b, err := dev.ReadBattery(); err == nil {
			rec.Event(EvBatteryLevel, map[string]any{"percent": b})
		}
	}()
	select {
	case <-done:
	case <-time.After(8 * time.Second):
	}
	dev.Close()
}
