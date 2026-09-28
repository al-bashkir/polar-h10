package polar

import (
	"encoding/binary"
	"fmt"
	"strings"
)

// PMD measurement types (first byte of data frames and command parameter).
const (
	MeasECG  byte = 0x00
	MeasPPG  byte = 0x01
	MeasACC  byte = 0x02
	MeasPPI  byte = 0x03
	MeasGyro byte = 0x05
	MeasMag  byte = 0x06
)

var measNames = map[byte]string{
	MeasECG:  "ecg",
	MeasPPG:  "ppg",
	MeasACC:  "acc",
	MeasPPI:  "ppi",
	MeasGyro: "gyro",
	MeasMag:  "magnetometer",
}

// MeasurementName returns a lowercase name for a PMD measurement type.
func MeasurementName(t byte) string {
	if n, ok := measNames[t]; ok {
		return n
	}
	return fmt.Sprintf("type_%d", t)
}

// PMD control point op codes.
const (
	opGetSettings byte = 0x01
	opStart       byte = 0x02
	opStop        byte = 0x03

	cpFeatureRead byte = 0x0F // first byte of a control point read
	cpResponse    byte = 0xF0 // first byte of a control point indication
)

// PMD setting types used in settings responses and start commands.
const (
	SettingSampleRate byte = 0x00
	SettingResolution byte = 0x01
	SettingRange      byte = 0x02
	SettingRangeMilli byte = 0x03
	SettingChannels   byte = 0x04
	SettingFactor     byte = 0x05
)

// settingSize is the byte width of a single value for each setting type.
var settingSize = map[byte]int{
	SettingSampleRate: 2,
	SettingResolution: 2,
	SettingRange:      2,
	SettingRangeMilli: 4,
	SettingChannels:   1,
	SettingFactor:     4,
}

// Features is the decoded PMD control point read value: the set of
// measurement types the device can stream.
type Features map[byte]bool

// ParseFeatures decodes the value read from the PMD control point:
// byte 0 = 0x0F, bytes 1.. = little-endian bitmask of measurement types.
func ParseFeatures(b []byte) (Features, error) {
	if len(b) < 2 {
		return nil, fmt.Errorf("pmd features: %w (%d bytes)", ErrShortPacket, len(b))
	}
	if b[0] != cpFeatureRead {
		return nil, fmt.Errorf("pmd features: unexpected header 0x%02x", b[0])
	}
	f := Features{}
	for byteIdx, v := range b[1:] {
		for bit := range 8 {
			if v&(1<<bit) != 0 {
				f[byte(byteIdx*8+bit)] = true
			}
		}
	}
	return f, nil
}

// String lists supported measurements, e.g. "ecg,acc".
func (f Features) String() string {
	var names []string
	for t := range byte(32) {
		if f[t] {
			names = append(names, MeasurementName(t))
		}
	}
	return strings.Join(names, ",")
}

// PMD control point response error codes.
var pmdErrors = map[byte]string{
	0:  "success",
	1:  "invalid op code",
	2:  "invalid measurement type",
	3:  "not supported",
	4:  "invalid length",
	5:  "invalid parameter",
	6:  "already in state",
	7:  "invalid resolution",
	8:  "invalid sample rate",
	9:  "invalid range",
	10: "invalid MTU",
	11: "invalid number of channels",
	12: "invalid state",
	13: "device in charger",
}

const statusAlreadyInState byte = 6

// PMDError is a non-success status returned by the PMD control point.
type PMDError struct {
	Op     byte
	Meas   byte
	Status byte
}

func (e *PMDError) Error() string {
	msg, ok := pmdErrors[e.Status]
	if !ok {
		msg = "unknown error"
	}
	return fmt.Sprintf("pmd op 0x%02x (%s): status %d: %s", e.Op, MeasurementName(e.Meas), e.Status, msg)
}

// CPResponse is a decoded PMD control point indication.
type CPResponse struct {
	Op     byte
	Meas   byte
	Status byte
	More   bool   // more response frames follow
	Params []byte // op-specific parameters
}

// Err returns a *PMDError if Status is not success.
func (r CPResponse) Err() error {
	if r.Status == 0 {
		return nil
	}
	return &PMDError{Op: r.Op, Meas: r.Meas, Status: r.Status}
}

// ParseCPResponse decodes a PMD control point indication:
// [0xF0, op, measurement type, status, more, params...].
func ParseCPResponse(b []byte) (CPResponse, error) {
	var r CPResponse
	if len(b) < 4 {
		return r, fmt.Errorf("pmd response: %w (%d bytes)", ErrShortPacket, len(b))
	}
	if b[0] != cpResponse {
		return r, fmt.Errorf("pmd response: unexpected header 0x%02x", b[0])
	}
	r.Op, r.Meas, r.Status = b[1], b[2], b[3]
	if len(b) > 4 {
		r.More = b[4] != 0
		r.Params = b[5:]
	}
	return r, nil
}

// Settings maps a setting type to its available (or selected) values.
type Settings map[byte][]uint32

// ParseSettings decodes a sequence of [type, count, count*value] entries as
// found in a get-settings response. Values are little-endian with a width
// that depends on the setting type.
func ParseSettings(b []byte) (Settings, error) {
	s := Settings{}
	for i := 0; i < len(b); {
		if len(b) < i+2 {
			return s, fmt.Errorf("pmd settings: %w at offset %d", ErrShortPacket, i)
		}
		typ, count := b[i], int(b[i+1])
		i += 2
		size, ok := settingSize[typ]
		if !ok {
			return s, fmt.Errorf("pmd settings: unknown setting type 0x%02x", typ)
		}
		if len(b) < i+count*size {
			return s, fmt.Errorf("pmd settings: %w for type 0x%02x", ErrShortPacket, typ)
		}
		for range count {
			var v uint32
			switch size {
			case 1:
				v = uint32(b[i])
			case 2:
				v = uint32(binary.LittleEndian.Uint16(b[i:]))
			case 4:
				v = binary.LittleEndian.Uint32(b[i:])
			}
			s[typ] = append(s[typ], v)
			i += size
		}
	}
	return s, nil
}

// GetSettingsCommand builds a "get measurement settings" command.
func GetSettingsCommand(meas byte) []byte {
	return []byte{opGetSettings, meas}
}

// StartECGCommand builds the command that starts ECG streaming with the given
// sample rate (Hz) and resolution (bits). The H10 supports 130 Hz / 14 bit.
func StartECGCommand(sampleRate, resolution uint16) []byte {
	b := []byte{opStart, MeasECG, SettingSampleRate, 1, 0, 0, SettingResolution, 1, 0, 0}
	binary.LittleEndian.PutUint16(b[4:], sampleRate)
	binary.LittleEndian.PutUint16(b[8:], resolution)
	return b
}

// StopCommand builds the command that stops streaming a measurement type.
func StopCommand(meas byte) []byte {
	return []byte{opStop, meas}
}
