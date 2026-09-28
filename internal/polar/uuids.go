// Package polar implements the subset of the Polar H10 BLE protocol used by
// h10: standard GATT heart rate, battery and device information, and the
// Polar Measurement Data (PMD) service for raw ECG streaming.
//
// All Polar-specific binary parsing lives in this package. See
// docs/polar-protocol.md for the protocol description and assumptions.
package polar

// GATT UUIDs, in canonical lowercase 128-bit form.
const (
	HeartRateService     = "0000180d-0000-1000-8000-00805f9b34fb"
	HeartRateMeasurement = "00002a37-0000-1000-8000-00805f9b34fb"
	BatteryService       = "0000180f-0000-1000-8000-00805f9b34fb"
	BatteryLevel         = "00002a19-0000-1000-8000-00805f9b34fb"
	DeviceInfoService    = "0000180a-0000-1000-8000-00805f9b34fb"
	ManufacturerName     = "00002a29-0000-1000-8000-00805f9b34fb"
	ModelNumber          = "00002a24-0000-1000-8000-00805f9b34fb"
	SerialNumber         = "00002a25-0000-1000-8000-00805f9b34fb"
	HardwareRevision     = "00002a27-0000-1000-8000-00805f9b34fb"
	FirmwareRevision     = "00002a26-0000-1000-8000-00805f9b34fb"
	SoftwareRevision     = "00002a28-0000-1000-8000-00805f9b34fb"
	PMDService           = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
	PMDControlPoint      = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
	PMDData              = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"
)

// CharName returns a short human-readable name for a characteristic UUID,
// used in raw capture files and logs.
func CharName(uuid string) string {
	switch uuid {
	case HeartRateMeasurement:
		return "hr_measurement"
	case BatteryLevel:
		return "battery_level"
	case PMDControlPoint:
		return "pmd_control"
	case PMDData:
		return "pmd_data"
	case ManufacturerName:
		return "manufacturer_name"
	case ModelNumber:
		return "model_number"
	case SerialNumber:
		return "serial_number"
	case HardwareRevision:
		return "hardware_revision"
	case FirmwareRevision:
		return "firmware_revision"
	case SoftwareRevision:
		return "software_revision"
	}
	return uuid
}
