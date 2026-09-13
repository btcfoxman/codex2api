//go:build !windows

package proxy

import "os"

func createContinuousRetryReplayFile() (*os.File, error) {
	file, err := os.CreateTemp("", "codex2api-continuous-retry-*")
	if err != nil {
		return nil, err
	}
	// The descriptor is sufficient for replay. Unlink immediately so even a
	// crashed process cannot leave a response on disk.
	if err := os.Remove(file.Name()); err != nil {
		_ = file.Close()
		_ = os.Remove(file.Name())
		return nil, err
	}
	return file, nil
}
