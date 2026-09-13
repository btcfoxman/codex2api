package admin

import (
	"context"
	"net/http"
	"testing"

	"github.com/codex2api/internal/imagestore"
)

func TestImageStoragePublicURLSettingsRoundTrip(t *testing.T) {
	previous := imagestore.CurrentConfig()
	t.Cleanup(func() { _ = imagestore.Configure(previous) })
	t.Setenv("IMAGE_ASSET_DIR", t.TempDir())
	handler, db, _ := newImagesSettingsHandler(t)
	for _, step := range []struct {
		patch map[string]any
		want  string
	}{
		{map[string]any{
			"image_storage_backend":    "s3",
			"image_s3_endpoint":        "https://s3.example.com",
			"image_s3_bucket":          "test-images",
			"image_s3_access_key":      "test-access-key",
			"image_s3_secret_key":      "test-secret-key",
			"image_s3_prefix":          "images",
			"image_s3_public_base_url": " https://cdn.example.com/images/ ",
		}, "https://cdn.example.com/images"},
		{map[string]any{"site_name": "unrelated update"}, "https://cdn.example.com/images"},
		{map[string]any{"image_s3_public_base_url": ""}, ""},
	} {
		response := invokeResponseCacheSettingsAdmin(t, handler, http.MethodPut, step.patch)
		if response.Code != http.StatusOK {
			t.Fatalf("PUT status=%d: %s", response.Code, response.Body.String())
		}
		if got := decodeResponseCacheSettingsResponse(t, response).ImageS3PublicBaseURL; got != step.want {
			t.Fatalf("PUT public base URL = %q, want %q", got, step.want)
		}
		persisted, err := db.GetSystemSettings(context.Background())
		if err != nil {
			t.Fatal(err)
		}
		cfg, err := imagestore.ParseConfigJSON(persisted.ImageStorageConfig)
		if err != nil || cfg.PublicBaseURL != step.want {
			t.Fatalf("persisted public base URL = %q, want %q; err=%v", cfg.PublicBaseURL, step.want, err)
		}
		if err := imagestore.Configure(cfg); err != nil {
			t.Fatal(err)
		}
		get := invokeResponseCacheSettingsAdmin(t, handler, http.MethodGet, nil)
		if get.Code != http.StatusOK || decodeResponseCacheSettingsResponse(t, get).ImageS3PublicBaseURL != step.want {
			t.Fatalf("GET after config reload failed: %d %s", get.Code, get.Body.String())
		}
		url, ok := imagestore.PublicURL("s3://test-images/images/example.png")
		if step.want == "" {
			if ok {
				t.Fatalf("cleared public URL remained active: %s", url)
			}
		} else if !ok || url != "https://cdn.example.com/images/example.png" {
			t.Fatalf("public URL = %q, ok=%t", url, ok)
		}
	}
}
