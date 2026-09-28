package polar

import (
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

// Link is a connected GATT client. It isolates the platform BLE stack so the
// protocol logic can be tested without hardware. Characteristics are
// identified by canonical lowercase 128-bit UUID strings.
type Link interface {
	Has(char string) bool
	Read(char string) ([]byte, error)
	Write(char string, data []byte) error
	// Subscribe enables notifications or indications. fn is called from the
	// BLE stack's goroutine and must not block; data is only valid during
	// the call.
	Subscribe(char string, fn func(data []byte)) error
	Connected() bool
	Close() error
}

// Packet is a raw BLE frame exchanged with the device, stamped with the host
// time at which it was received (or sent, for TX).
type Packet struct {
	Received time.Time
	Char     string // characteristic UUID
	TX       bool   // true for frames written by the host
	Data     []byte
}

// Info describes a connected device.
type Info struct {
	Name         string
	ID           string
	Address      string
	Manufacturer string
	Model        string
	Serial       string
	Hardware     string
	Firmware     string
	Software     string
	Battery      int // percent, -1 if unknown
	Features     Features
	HasHR        bool
}

// NamePrefix is the advertised local name prefix of Polar H10 sensors.
const NamePrefix = "Polar H10"

// IsH10 reports whether an advertised local name belongs to a Polar H10.
func IsH10(name string) bool {
	return strings.HasPrefix(name, NamePrefix+" ")
}

// DeviceID extracts the Polar device ID from an advertised name such as
// "Polar H10 ABC12345". It returns "" if there is none.
func DeviceID(name string) string {
	f := strings.Fields(name)
	if len(f) < 3 {
		return ""
	}
	return f[len(f)-1]
}

// ErrNotSupported is returned when the device lacks a required feature.
var ErrNotSupported = errors.New("not supported by device")

// cpTimeout bounds the wait for a control point response (variable for tests).
var cpTimeout = 5 * time.Second

// Device drives a connected Polar H10 over a Link.
type Device struct {
	Info Info

	link Link
	sink func(Packet)

	cmdMu  sync.Mutex // serializes control point commands
	cpResp chan CPResponse

	pmdOnce sync.Once
	pmdErr  error
}

// Open reads device information from a connected link. sink receives every
// notification, indication and command frame; it is called from BLE
// callbacks and must not block.
func Open(link Link, name, address string, sink func(Packet)) (*Device, error) {
	if sink == nil {
		sink = func(Packet) {}
	}
	d := &Device{
		link:   link,
		sink:   sink,
		cpResp: make(chan CPResponse, 4),
		Info: Info{
			Name:    name,
			ID:      DeviceID(name),
			Address: address,
			Battery: -1,
			HasHR:   link.Has(HeartRateMeasurement),
		},
	}
	for char, dst := range map[string]*string{
		ManufacturerName: &d.Info.Manufacturer,
		ModelNumber:      &d.Info.Model,
		SerialNumber:     &d.Info.Serial,
		HardwareRevision: &d.Info.Hardware,
		FirmwareRevision: &d.Info.Firmware,
		SoftwareRevision: &d.Info.Software,
	} {
		if !link.Has(char) {
			continue
		}
		if b, err := link.Read(char); err == nil {
			*dst = strings.TrimRight(string(b), "\x00 ")
		}
	}
	if b, err := d.ReadBattery(); err == nil {
		d.Info.Battery = b
	}
	if link.Has(PMDControlPoint) {
		b, err := link.Read(PMDControlPoint)
		if err != nil {
			return nil, fmt.Errorf("read pmd features: %w", err)
		}
		f, err := ParseFeatures(b)
		if err != nil {
			return nil, err
		}
		d.Info.Features = f
	}
	return d, nil
}

// ReadBattery reads the battery level in percent.
func (d *Device) ReadBattery() (int, error) {
	if !d.link.Has(BatteryLevel) {
		return -1, fmt.Errorf("battery level: %w", ErrNotSupported)
	}
	b, err := d.link.Read(BatteryLevel)
	if err != nil {
		return -1, fmt.Errorf("read battery: %w", err)
	}
	if len(b) < 1 {
		return -1, fmt.Errorf("battery level: %w", ErrShortPacket)
	}
	return int(b[0]), nil
}

// forward returns a notification callback that copies the frame, stamps it
// and passes it to the sink.
func (d *Device) forward(char string, extra func([]byte)) func([]byte) {
	return func(data []byte) {
		now := time.Now()
		buf := append([]byte(nil), data...)
		if extra != nil {
			extra(buf)
		}
		d.sink(Packet{Received: now, Char: char, Data: buf})
	}
}

// StartHR subscribes to heart rate measurement notifications.
func (d *Device) StartHR() error {
	if !d.Info.HasHR {
		return fmt.Errorf("heart rate: %w", ErrNotSupported)
	}
	if err := d.link.Subscribe(HeartRateMeasurement, d.forward(HeartRateMeasurement, nil)); err != nil {
		return fmt.Errorf("subscribe heart rate: %w", err)
	}
	return nil
}

// enablePMD subscribes to the PMD control point and data characteristics.
func (d *Device) enablePMD() error {
	d.pmdOnce.Do(func() {
		if !d.link.Has(PMDControlPoint) || !d.link.Has(PMDData) {
			d.pmdErr = fmt.Errorf("pmd service: %w", ErrNotSupported)
			return
		}
		err := d.link.Subscribe(PMDControlPoint, d.forward(PMDControlPoint, func(b []byte) {
			r, err := ParseCPResponse(b)
			if err != nil {
				return // e.g. a feature read echoed through the callback on macOS
			}
			select {
			case d.cpResp <- r:
			default:
			}
		}))
		if err != nil {
			d.pmdErr = fmt.Errorf("subscribe pmd control point: %w", err)
			return
		}
		if err := d.link.Subscribe(PMDData, d.forward(PMDData, nil)); err != nil {
			d.pmdErr = fmt.Errorf("subscribe pmd data: %w", err)
		}
	})
	return d.pmdErr
}

// command writes a control point command and waits for its response.
func (d *Device) command(cmd []byte) (CPResponse, error) {
	if err := d.enablePMD(); err != nil {
		return CPResponse{}, err
	}
	d.cmdMu.Lock()
	defer d.cmdMu.Unlock()

	// Discard stale responses from earlier timed-out commands.
	for len(d.cpResp) > 0 {
		<-d.cpResp
	}
	d.sink(Packet{Received: time.Now(), Char: PMDControlPoint, TX: true, Data: append([]byte(nil), cmd...)})
	if err := d.link.Write(PMDControlPoint, cmd); err != nil {
		return CPResponse{}, fmt.Errorf("write pmd command 0x%02x: %w", cmd[0], err)
	}
	timeout := time.After(cpTimeout)
	for {
		select {
		case r := <-d.cpResp:
			if r.Op != cmd[0] {
				continue
			}
			return r, nil
		case <-timeout:
			return CPResponse{}, fmt.Errorf("pmd command 0x%02x: no response within %s", cmd[0], cpTimeout)
		}
	}
}

// ECGSettings queries the ECG settings supported by the device.
func (d *Device) ECGSettings() (Settings, error) {
	if !d.Info.Features[MeasECG] {
		return nil, fmt.Errorf("ecg: %w", ErrNotSupported)
	}
	r, err := d.command(GetSettingsCommand(MeasECG))
	if err != nil {
		return nil, err
	}
	if err := r.Err(); err != nil {
		return nil, err
	}
	return ParseSettings(r.Params)
}

// StartECG starts ECG streaming at the H10 sample rate and resolution.
// Samples arrive as PMD data packets through the sink.
func (d *Device) StartECG() error {
	if !d.Info.Features[MeasECG] {
		return fmt.Errorf("ecg: %w", ErrNotSupported)
	}
	r, err := d.command(StartECGCommand(ECGSampleRate, ECGResolution))
	if err != nil {
		return err
	}
	if r.Status == statusAlreadyInState {
		return nil
	}
	return r.Err()
}

// StopECG stops ECG streaming.
func (d *Device) StopECG() error {
	r, err := d.command(StopCommand(MeasECG))
	if err != nil {
		return err
	}
	if r.Status == statusAlreadyInState {
		return nil
	}
	return r.Err()
}

// Connected reports whether the link is still up.
func (d *Device) Connected() bool { return d.link.Connected() }

// Close disconnects from the device.
func (d *Device) Close() error { return d.link.Close() }
