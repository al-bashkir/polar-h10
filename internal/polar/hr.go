package polar

import (
	"encoding/binary"
	"errors"
	"fmt"
)

// HRMeasurement is a decoded GATT Heart Rate Measurement (0x2A37) notification.
type HRMeasurement struct {
	HeartRate int // beats per minute

	ContactSupported bool
	ContactDetected  bool // meaningful only if ContactSupported

	HasEnergy      bool
	EnergyExpended int // kJ, only if HasEnergy

	// RR holds RR intervals in the raw 1/1024 s units sent by the sensor,
	// oldest first. Use RRMillis for conversion.
	RR []uint16
}

// RRMillis converts a raw RR interval (1/1024 s) to milliseconds. The result
// is exact in float64 (raw * 0.9765625).
func RRMillis(raw uint16) float64 {
	return float64(raw) * 1000 / 1024
}

// RRNanos converts a raw RR interval (1/1024 s) to nanoseconds, rounded to the
// nearest nanosecond (1 unit = 976562.5 ns, so the error is at most 0.5 ns).
func RRNanos(raw uint16) int64 {
	return (int64(raw)*1_000_000_000 + 512) / 1024
}

var ErrShortPacket = errors.New("packet too short")

// ParseHR decodes a Heart Rate Measurement characteristic value as defined in
// the Bluetooth GATT Heart Rate Service specification.
func ParseHR(b []byte) (HRMeasurement, error) {
	var m HRMeasurement
	if len(b) < 2 {
		return m, fmt.Errorf("hr measurement: %w (%d bytes)", ErrShortPacket, len(b))
	}
	flags := b[0]
	i := 1

	if flags&0x01 != 0 { // uint16 heart rate
		if len(b) < i+2 {
			return m, fmt.Errorf("hr measurement: %w for uint16 heart rate", ErrShortPacket)
		}
		m.HeartRate = int(binary.LittleEndian.Uint16(b[i:]))
		i += 2
	} else {
		m.HeartRate = int(b[i])
		i++
	}

	// Bits 1-2: sensor contact status. 0b10 = supported, not detected;
	// 0b11 = supported, detected; 0b0x = not supported.
	m.ContactSupported = flags&0x04 != 0
	m.ContactDetected = m.ContactSupported && flags&0x02 != 0

	if flags&0x08 != 0 {
		if len(b) < i+2 {
			return m, fmt.Errorf("hr measurement: %w for energy expended", ErrShortPacket)
		}
		m.HasEnergy = true
		m.EnergyExpended = int(binary.LittleEndian.Uint16(b[i:]))
		i += 2
	}

	if flags&0x10 != 0 {
		rest := b[i:]
		if len(rest)%2 != 0 {
			return m, fmt.Errorf("hr measurement: odd RR payload length %d", len(rest))
		}
		for j := 0; j < len(rest); j += 2 {
			m.RR = append(m.RR, binary.LittleEndian.Uint16(rest[j:]))
		}
	}
	return m, nil
}
