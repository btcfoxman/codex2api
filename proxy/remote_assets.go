package proxy

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"net/url"
	"strings"
	"time"
)

const (
	remoteAssetInlineMaxBytes = 25 * 1024 * 1024
	remoteAssetInlineTimeout  = 45 * time.Second
)

type remoteAssetInlineCache struct {
	client *http.Client
	items  map[string]string
}

func isUpstreamFileDownload407Error(statusCode int, body []byte) bool {
	if statusCode < 400 || len(body) == 0 {
		return false
	}
	text := strings.ToLower(string(body))
	return strings.Contains(text, "error while downloading file") &&
		strings.Contains(text, "407") &&
		(strings.Contains(text, "upstream status code") || strings.Contains(text, "status code"))
}

func inlineRemoteAssetsFor407Retry(ctx context.Context, endpoint string, body []byte, proxyURL string, statusCode int, errBody []byte, accountID int64) ([]byte, bool) {
	if !isUpstreamFileDownload407Error(statusCode, errBody) {
		return body, false
	}
	inlined, changed, err := inlineRemoteAssetURLs(ctx, body, proxyURL)
	if err != nil {
		log.Printf("[remote-assets] %s account=%d asset inline failed after upstream download 407: %v", endpoint, accountID, err)
		return body, false
	}
	if changed {
		log.Printf("[remote-assets] %s account=%d inlined remote asset URLs after upstream download 407", endpoint, accountID)
	}
	return inlined, changed
}

func inlineRemoteAssetURLs(ctx context.Context, body []byte, proxyURL string) ([]byte, bool, error) {
	var root any
	if err := json.Unmarshal(body, &root); err != nil {
		return body, false, err
	}
	cache := &remoteAssetInlineCache{
		client: &http.Client{
			Transport: newCodexStandardTransport(proxyURL),
			Timeout:   remoteAssetInlineTimeout,
		},
		items: make(map[string]string),
	}
	changed, err := inlineRemoteAssetsInValue(ctx, root, cache)
	if err != nil {
		return body, false, err
	}
	if !changed {
		return body, false, nil
	}
	out, err := json.Marshal(root)
	if err != nil {
		return body, false, err
	}
	return out, true, nil
}

func inlineRemoteAssetsInValue(ctx context.Context, value any, cache *remoteAssetInlineCache) (bool, error) {
	switch v := value.(type) {
	case map[string]any:
		return inlineRemoteAssetsInMap(ctx, v, cache)
	case []any:
		changed := false
		for _, item := range v {
			itemChanged, err := inlineRemoteAssetsInValue(ctx, item, cache)
			if err != nil {
				return changed, err
			}
			changed = changed || itemChanged
		}
		return changed, nil
	default:
		return false, nil
	}
}

func inlineRemoteAssetsInMap(ctx context.Context, item map[string]any, cache *remoteAssetInlineCache) (bool, error) {
	changed := false

	if raw, ok := item["image_url"]; ok {
		if imageURL, ok := remoteAssetURLString(raw); ok {
			dataURL, err := cache.fetchDataURL(ctx, imageURL, true)
			if err != nil {
				return changed, err
			}
			item["image_url"] = dataURL
			changed = true
		}
	}

	if raw, ok := item["file_url"]; ok {
		if fileURL, ok := remoteAssetURLString(raw); ok {
			dataURL, err := cache.fetchDataURL(ctx, fileURL, false)
			if err != nil {
				return changed, err
			}
			item["file_data"] = dataURL
			delete(item, "file_url")
			changed = true
		}
	}

	for _, raw := range item {
		itemChanged, err := inlineRemoteAssetsInValue(ctx, raw, cache)
		if err != nil {
			return changed, err
		}
		changed = changed || itemChanged
	}
	return changed, nil
}

func remoteAssetURLString(raw any) (string, bool) {
	var candidate string
	switch v := raw.(type) {
	case string:
		candidate = strings.TrimSpace(v)
	case map[string]any:
		candidate = strings.TrimSpace(firstNonEmptyAnyString(v["url"]))
	default:
		return "", false
	}
	if candidate == "" || strings.HasPrefix(strings.ToLower(candidate), "data:") {
		return "", false
	}
	parsed, err := url.Parse(candidate)
	if err != nil {
		return "", false
	}
	switch strings.ToLower(parsed.Scheme) {
	case "http", "https":
		return candidate, true
	default:
		return "", false
	}
}

func (c *remoteAssetInlineCache) fetchDataURL(ctx context.Context, rawURL string, requireImage bool) (string, error) {
	if cached, ok := c.items[rawURL]; ok {
		return cached, nil
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", err
	}
	req.Header.Set("User-Agent", MinimalCodexCLIUserAgentForHeaders())
	resp, err := c.client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("download remote asset %s failed: status %d", safeRemoteAssetURLForLog(rawURL), resp.StatusCode)
	}
	limited := io.LimitReader(resp.Body, remoteAssetInlineMaxBytes+1)
	data, err := io.ReadAll(limited)
	if err != nil {
		return "", err
	}
	if len(data) > remoteAssetInlineMaxBytes {
		return "", fmt.Errorf("download remote asset %s exceeded %d bytes", safeRemoteAssetURLForLog(rawURL), remoteAssetInlineMaxBytes)
	}
	if len(data) == 0 {
		return "", fmt.Errorf("download remote asset %s returned empty body", safeRemoteAssetURLForLog(rawURL))
	}
	contentType := strings.TrimSpace(strings.Split(resp.Header.Get("Content-Type"), ";")[0])
	if contentType == "" || strings.EqualFold(contentType, "application/octet-stream") {
		contentType = http.DetectContentType(data)
	}
	if requireImage && !strings.HasPrefix(strings.ToLower(contentType), "image/") {
		return "", fmt.Errorf("download remote image %s returned non-image content type %q", safeRemoteAssetURLForLog(rawURL), contentType)
	}
	dataURL := "data:" + contentType + ";base64," + base64.StdEncoding.EncodeToString(data)
	c.items[rawURL] = dataURL
	return dataURL, nil
}

func safeRemoteAssetURLForLog(rawURL string) string {
	parsed, err := url.Parse(rawURL)
	if err != nil {
		return "<invalid-url>"
	}
	parsed.User = nil
	parsed.RawQuery = ""
	parsed.Fragment = ""
	return parsed.String()
}
