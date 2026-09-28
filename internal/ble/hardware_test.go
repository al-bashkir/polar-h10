//go:build hardware

// Hardware integration test: needs a Polar H10 worn and in range.
//
//	go test -tags hardware -v ./internal/ble
//	H10_DEVICE=ABC12345 go test -tags hardware -v ./internal/ble
package ble

import (
	"context"
	"os"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/al-bashkir/polar-h10/internal/polar"
)

func TestH10Streams(t *testing.T) {
	ad, err := Open()
	if err != nil {
		t.Fatal(err)
	}
	want := strings.ToUpper(os.Getenv("H10_DEVICE"))
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	var found *Advertisement
	var mu sync.Mutex
	if err := ad.Scan(ctx, func(a Advertisement) bool {
		mu.Lock()
		defer mu.Unlock()
		if found == nil && polar.IsH10(a.Name) && (want == "" || polar.DeviceID(a.Name) == want) {
			found = &a
		}
		return found == nil
	}); err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	adv := found
	mu.Unlock()
	if adv == nil {
		t.Fatal("no Polar H10 found")
	}
	conn, err := ad.Connect(context.Background(), *adv, 15*time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()

	var pmu sync.Mutex
	counts := map[string]int{}
	var errs []error
	dev, err := polar.Open(conn, adv.Name, adv.Address, func(p polar.Packet) {
		pmu.Lock()
		defer pmu.Unlock()
		counts[p.Char]++
		switch p.Char {
		case polar.HeartRateMeasurement:
			if _, err := polar.ParseHR(p.Data); err != nil {
				errs = append(errs, err)
			}
		case polar.PMDData:
			if _, err := polar.ParseECGFrame(p.Data); err != nil {
				errs = append(errs, err)
			}
		}
	})
	if err != nil {
		t.Fatal(err)
	}
	t.Logf("device: %+v", dev.Info)
	if !dev.Info.Features[polar.MeasECG] {
		t.Fatal("ECG not supported")
	}
	if err := dev.StartHR(); err != nil {
		t.Fatal(err)
	}
	if err := dev.StartECG(); err != nil {
		t.Fatal(err)
	}
	time.Sleep(10 * time.Second)
	if err := dev.StopECG(); err != nil {
		t.Error(err)
	}

	pmu.Lock()
	defer pmu.Unlock()
	t.Logf("packets: hr=%d ecg=%d", counts[polar.HeartRateMeasurement], counts[polar.PMDData])
	if counts[polar.HeartRateMeasurement] < 5 || counts[polar.PMDData] < 10 {
		t.Errorf("too few packets: %v", counts)
	}
	for _, err := range errs {
		t.Error(err)
	}
}
