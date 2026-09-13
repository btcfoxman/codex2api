package proxy

import (
	"crypto/rand"
	"errors"
	"os"
	"path/filepath"

	"golang.org/x/sys/windows"
)

func createContinuousRetryReplayFile() (*os.File, error) {
	// Windows cannot unlink an ordinary open file. Have the OS delete it when
	// the handle closes, including on process exit, without a close/reopen gap.
	for range 10 {
		name := filepath.Join(os.TempDir(), "codex2api-continuous-retry-"+rand.Text())
		file, err := os.OpenFile(name, os.O_RDWR|os.O_CREATE|os.O_EXCL|windows.O_FILE_FLAG_DELETE_ON_CLOSE, 0o600)
		if errors.Is(err, os.ErrExist) {
			continue
		}
		return file, err
	}
	return nil, os.ErrExist
}
