package proxy

import (
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/gin-gonic/gin"
)

func TestRemoteAsset407RetryAcrossHandlers(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, endpoint := range []string{"/v1/responses", "/v1/responses/compact", "/v1/chat/completions"} {
		for _, repeatedFailure := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s/repeated_failure=%t", endpoint, repeatedFailure), func(t *testing.T) {
				imageBytes := []byte{0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a}
				asset := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					w.Header().Set("Content-Type", "image/png")
					_, _ = w.Write(imageBytes)
				}))
				t.Cleanup(asset.Close)
				var attempts atomic.Int32
				bodies := make(chan string, 4)
				upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					body := string(readUpstreamRequestBody(r))
					select {
					case bodies <- body:
					default:
					}
					if attempts.Add(1) == 1 || repeatedFailure {
						w.Header().Set("Content-Type", "application/json")
						w.WriteHeader(http.StatusBadRequest)
						_, _ = io.WriteString(w, `{"error":{"message":"Error while downloading file. Upstream status code: 407.","type":"invalid_request_error","code":"invalid_value"}}`)
						return
					}
					response := `{"id":"resp_inline_test","object":"response","status":"completed","model":"gpt-4.1-direct","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"OK"}]}],"usage":{"input_tokens":10,"output_tokens":2}}`
					if endpoint == "/v1/responses/compact" {
						w.Header().Set("Content-Type", "application/json")
						_, _ = io.WriteString(w, response)
						return
					}
					w.Header().Set("Content-Type", "text/event-stream")
					_, _ = io.WriteString(w, "data: {\"type\":\"response.output_text.delta\",\"delta\":\"OK\"}\n\n")
					_, _ = fmt.Fprintf(w, "data: {\"type\":\"response.completed\",\"response\":%s}\n\n", response)
				}))
				t.Cleanup(upstream.Close)
				store := newOpenAIResponsesRelayStore(upstream.URL)
				t.Cleanup(store.Stop)
				handler := NewHandler(store, nil, nil, nil)
				body := fmt.Sprintf(`{"model":"gpt-4.1-direct","input":[{"role":"user","content":[{"type":"input_image","image_url":%q}]}],"stream":true}`, asset.URL+"/image.png")
				if endpoint == "/v1/chat/completions" {
					body = fmt.Sprintf(`{"model":"gpt-4.1-direct","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":%q}}]}],"stream":true}`, asset.URL+"/image.png")
				}
				recorder := httptest.NewRecorder()
				ctx, _ := gin.CreateTestContext(recorder)
				deadline, cancel := context.WithTimeout(context.Background(), 5*time.Second)
				defer cancel()
				ctx.Request = httptest.NewRequest(http.MethodPost, endpoint, strings.NewReader(body)).WithContext(deadline)
				ctx.Request.Header.Set("Content-Type", "application/json")
				switch endpoint {
				case "/v1/responses":
					handler.Responses(ctx)
				case "/v1/responses/compact":
					handler.ResponsesCompact(ctx)
				default:
					handler.ChatCompletions(ctx)
				}
				if got := attempts.Load(); got != 2 {
					t.Fatalf("upstream attempts = %d, want exactly 2; response=%s", got, recorder.Body.String())
				}
				first, second := <-bodies, <-bodies
				if !strings.Contains(first, asset.URL+"/image.png") {
					t.Fatalf("initial request lost remote asset: %s", first)
				}
				if strings.Contains(second, asset.URL) || !strings.Contains(second, "data:image/png;base64,"+base64.StdEncoding.EncodeToString(imageBytes)) {
					t.Fatalf("retry did not inline image bytes: %s", second)
				}
				if repeatedFailure {
					if !strings.Contains(recorder.Body.String(), "407") {
						t.Fatalf("repeated failure did not reach caller: %s", recorder.Body.String())
					}
				} else if recorder.Code != http.StatusOK || !strings.Contains(recorder.Body.String(), "OK") {
					t.Fatalf("retry failed: status=%d body=%s", recorder.Code, recorder.Body.String())
				}
			})
		}
	}
}
