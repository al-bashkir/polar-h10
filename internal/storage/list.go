package storage

import (
	"errors"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
)

// SessionEntry is a session directory found under the data directory.
type SessionEntry struct {
	Dir  string
	Meta *Metadata // nil if metadata.json is missing or unreadable
	Err  error
}

// ListSessions returns session directories under dataDir, newest first.
// A missing data directory yields an empty list.
func ListSessions(dataDir string) ([]SessionEntry, error) {
	entries, err := os.ReadDir(dataDir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []SessionEntry
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		dir := filepath.Join(dataDir, e.Name())
		m, err := ReadMetadata(dir)
		if errors.Is(err, fs.ErrNotExist) {
			continue // not a session directory
		}
		out = append(out, SessionEntry{Dir: dir, Meta: m, Err: err})
	}
	// Directory names start with the UTC start time, so they sort by time.
	sort.Slice(out, func(i, j int) bool { return filepath.Base(out[i].Dir) > filepath.Base(out[j].Dir) })
	return out, nil
}
