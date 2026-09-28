package cli

import (
	"fmt"
	"io"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/al-bashkir/polar-h10/internal/recorder"
)

// display renders a status block that is redrawn in place on a terminal.
// Without a terminal (or in verbose mode) it prints a compact line at a low
// rate instead. Log output is routed through it so log lines appear above
// the block instead of tearing it.
type display struct {
	mu       sync.Mutex
	w        io.Writer
	tty      bool
	lines    int // lines of the currently drawn block
	lastLine time.Time
}

func newDisplay(w io.Writer, tty bool) *display { return &display{w: w, tty: tty} }

func isTerminal(f *os.File) bool {
	fi, err := f.Stat()
	return err == nil && fi.Mode()&os.ModeCharDevice != 0 && os.Getenv("TERM") != "dumb"
}

// eraseLocked removes the drawn block, leaving the cursor where it began.
func (d *display) eraseLocked() {
	if d.lines > 0 {
		fmt.Fprintf(d.w, "\x1b[%dA\x1b[J", d.lines)
		d.lines = 0
	}
}

// show draws the block. In non-tty mode only summary is printed, at most
// every 10 seconds.
func (d *display) show(block []string, summary string) {
	d.mu.Lock()
	defer d.mu.Unlock()
	if !d.tty {
		if time.Since(d.lastLine) >= 10*time.Second {
			d.lastLine = time.Now()
			fmt.Fprintln(d.w, summary)
		}
		return
	}
	var b strings.Builder
	if d.lines > 0 {
		fmt.Fprintf(&b, "\x1b[%dA", d.lines)
	}
	for _, l := range block {
		b.WriteString("\r\x1b[K" + l + "\n")
	}
	b.WriteString("\x1b[J")
	io.WriteString(d.w, b.String())
	d.lines = len(block)
}

// clear removes the block (tty) so final output starts on a clean screen.
func (d *display) clear() {
	d.mu.Lock()
	defer d.mu.Unlock()
	if d.tty {
		d.eraseLocked()
	}
}

// done forgets the block, leaving the last drawn state on screen.
func (d *display) done() {
	d.mu.Lock()
	d.lines = 0
	d.mu.Unlock()
}

type logWriter struct {
	d   *display
	out io.Writer
}

// logWriter returns a writer for log output that cooperates with the block.
func (d *display) logWriter(out io.Writer) io.Writer { return logWriter{d, out} }

func (l logWriter) Write(p []byte) (int, error) {
	l.d.mu.Lock()
	defer l.d.mu.Unlock()
	if l.d.tty {
		l.d.eraseLocked() // next show() redraws below the log line
	}
	return l.out.Write(p)
}

func fmtClock(d time.Duration) string {
	d = d.Round(time.Second)
	return fmt.Sprintf("%02d:%02d:%02d", int(d.Hours()), int(d.Minutes())%60, int(d.Seconds())%60)
}

// statusBlock formats live status. extra lines are appended (e.g. session info).
func statusBlock(title string, s recorder.Status, started time.Time, ecgEnabled bool, extra ...string) ([]string, string) {
	now := time.Now()
	row := func(k, v string) string { return fmt.Sprintf("%-12s%s", k, v) }

	hr, rr := "--", "--"
	if !s.HRAt.IsZero() && now.Sub(s.HRAt) < 5*time.Second {
		hr = fmt.Sprintf("%d bpm", s.HR)
		if s.RRMs > 0 {
			rr = fmt.Sprintf("%.0f ms", s.RRMs)
		}
	}
	battery := "--"
	if s.Battery >= 0 {
		battery = fmt.Sprintf("%d %%", s.Battery)
	}
	conn := "connected"
	switch {
	case s.Connected:
	case s.Stats.Disconnects == 0:
		conn = "connecting"
	default:
		conn = "disconnected (reconnecting)"
	}
	contact := "--"
	if s.Contact != nil {
		contact = map[bool]string{true: "ok", false: "NO CONTACT"}[*s.Contact]
	}
	ecg := "off"
	switch {
	case !ecgEnabled:
	case s.ECGStreaming && now.Sub(s.LastECG) < 3*time.Second:
		ecg = "streaming"
	case s.ECGStreaming:
		ecg = "stalled"
	default:
		ecg = "starting"
	}
	st := s.Stats
	lost := st.DroppedPackets + st.DecodeErrors
	block := []string{
		title,
		strings.Repeat("─", 32),
		row("Status", conn),
		row("HR", hr),
		row("RR", rr),
		row("Contact", contact),
		row("Battery", battery),
		row("ECG", ecg),
		row("Packets", fmt.Sprint(st.BLEPackets)),
		row("Dropped", fmt.Sprint(st.DroppedPackets)),
		row("Duration", fmtClock(now.Sub(started))),
	}
	if st.DecodeErrors > 0 || st.ECGGaps > 0 || st.Reconnections > 0 {
		block = append(block, row("Anomalies", fmt.Sprintf("decode errors %d, ecg gaps %d (~%d samples), reconnects %d",
			st.DecodeErrors, st.ECGGaps, st.ECGMissingSamplesEst, st.Reconnections)))
	}
	block = append(block, extra...)
	summary := fmt.Sprintf("%s hr=%s rr=%s ecg=%s packets=%d dropped/errors=%d duration=%s",
		conn, hr, rr, ecg, st.BLEPackets, lost, fmtClock(now.Sub(started)))
	return block, summary
}
