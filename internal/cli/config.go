package cli

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// Config holds optional settings from the config file. CLI flags override it.
type Config struct {
	DataDir           string `json:"data_dir"`
	PreferredDevice   string `json:"preferred_device"`
	RawRecording      *bool  `json:"raw_recording"`
	ReconnectAttempts *int   `json:"reconnect_attempts"`
}

func defaultConfigPath() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		return ""
	}
	return filepath.Join(dir, "h10", "config.json")
}

// loadConfig reads a JSON config file. A missing default file is fine; a
// missing explicitly requested file is an error.
func loadConfig(path string, explicit bool) (Config, error) {
	var c Config
	if path == "" {
		path = defaultConfigPath()
	}
	if path == "" {
		return c, nil
	}
	b, err := os.ReadFile(expandHome(path))
	if errors.Is(err, fs.ErrNotExist) && !explicit {
		return c, nil
	}
	if err != nil {
		return c, fmt.Errorf("read config: %w", err)
	}
	dec := json.NewDecoder(strings.NewReader(string(b)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&c); err != nil {
		return c, fmt.Errorf("parse config %s: %w", path, err)
	}
	return c, nil
}

func (c Config) dataDir(flagValue string) string {
	d := flagValue
	if d == "" {
		d = c.DataDir
	}
	if d == "" {
		d = "~/h10-data"
	}
	return expandHome(d)
}

func expandHome(p string) string {
	if p == "~" || strings.HasPrefix(p, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, p[1:])
		}
	}
	return p
}
