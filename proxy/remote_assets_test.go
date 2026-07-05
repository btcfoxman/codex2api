package proxy

import (
	"context"
	"encoding/base64"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/tidwall/gjson"
)

func TestIsUpstreamFileDownload407Error(t *testing.T) {
	body := []byte(`{"error":{"message":"Error while downloading file. Upstream status code: 407.","type":"invalid_request_error","param":"url","code":"invalid_value"}}`)
	if !isUpstreamFileDownload407Error(http.StatusBadRequest, body) {
		t.Fatal("expected upstream file download 407 to be detected")
	}
	if isUpstreamFileDownload407Error(http.StatusBadRequest, []byte(`{"error":{"message":"other"}}`)) {
		t.Fatal("unexpected detection for unrelated 400")
	}
}

func TestInlineRemoteAssetURLsConvertsImageAndFileURLs(t *testing.T) {
	imageBytes := []byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a}
	fileBytes := []byte("hello file")
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/image.png":
			w.Header().Set("Content-Type", "image/png")
			_, _ = w.Write(imageBytes)
		case "/doc.txt":
			w.Header().Set("Content-Type", "text/plain")
			_, _ = w.Write(fileBytes)
		default:
			http.NotFound(w, r)
		}
	}))
	defer server.Close()

	body := []byte(`{
		"input": [{
			"role": "user",
			"content": [
				{"type": "input_image", "image_url": "` + server.URL + `/image.png"},
				{"type": "input_file", "file_url": "` + server.URL + `/doc.txt", "filename": "doc.txt"}
			]
		}]
	}`)

	out, changed, err := inlineRemoteAssetURLs(context.Background(), body, "")
	if err != nil {
		t.Fatalf("inlineRemoteAssetURLs returned error: %v", err)
	}
	if !changed {
		t.Fatal("expected body to change")
	}

	imageURL := gjson.GetBytes(out, "input.0.content.0.image_url").String()
	if !strings.HasPrefix(imageURL, "data:image/png;base64,") {
		t.Fatalf("image_url = %q, want image data URL", imageURL)
	}
	if got := strings.TrimPrefix(imageURL, "data:image/png;base64,"); got != base64.StdEncoding.EncodeToString(imageBytes) {
		t.Fatalf("image data mismatch")
	}

	fileData := gjson.GetBytes(out, "input.0.content.1.file_data").String()
	if !strings.HasPrefix(fileData, "data:text/plain;base64,") {
		t.Fatalf("file_data = %q, want text data URL", fileData)
	}
	if got := strings.TrimPrefix(fileData, "data:text/plain;base64,"); got != base64.StdEncoding.EncodeToString(fileBytes) {
		t.Fatalf("file data mismatch")
	}
	if gjson.GetBytes(out, "input.0.content.1.file_url").Exists() {
		t.Fatalf("file_url should be removed after inlining: %s", string(out))
	}
}
