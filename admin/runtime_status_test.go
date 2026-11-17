package admin

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/codex2api/proxy"
	"github.com/gin-gonic/gin"
)

// Both values must remain explicit JSON booleans: omitting false would make a
// deployment unable to distinguish an unready guard from an older server.
func TestRuntimeStatusOutputGuardReadinessJSONContract(t *testing.T) {
	for _, ready := range []bool{false, true} {
		encoded, err := json.Marshal(runtimeStatusResponse{OutputGuardReady: ready})
		if err != nil {
			t.Fatal(err)
		}
		var payload map[string]any
		if err := json.Unmarshal(encoded, &payload); err != nil {
			t.Fatal(err)
		}
		if value, ok := payload["output_guard_ready"].(bool); !ok || value != ready {
			t.Fatalf("output_guard_ready = %#v, want explicit boolean %v", payload["output_guard_ready"], ready)
		}
	}
}

// Read the real getter without modifying its process-wide readiness. The proxy
// tests cover cold-start false -> true; changing it here would pollute other
// admin tests because production only advances the guard generation.
func TestRuntimeStatusRouteOutputGuardReadinessMatchesProxy(t *testing.T) {
	gin.SetMode(gin.TestMode)
	handler := &Handler{adminSecretEnv: "runtime-status-test-secret", adminAuthThrottle: newAdminAuthThrottle()}
	router := gin.New()
	handler.RegisterRoutes(router)

	unauthorized := httptest.NewRecorder()
	router.ServeHTTP(unauthorized, httptest.NewRequest(http.MethodGet, "/api/admin/runtime-status", nil))
	if unauthorized.Code != http.StatusUnauthorized {
		t.Fatalf("unauthed runtime status = %d, want 401", unauthorized.Code)
	}
	if bytes.Contains(unauthorized.Body.Bytes(), []byte("output_guard_ready")) {
		t.Fatal("runtime readiness was exposed without admin authentication")
	}

	want := proxy.IsOutputGuardReady()
	request := httptest.NewRequest(http.MethodGet, "/api/admin/runtime-status", nil)
	request.Header.Set("X-Admin-Key", "runtime-status-test-secret")
	response := httptest.NewRecorder()
	router.ServeHTTP(response, request)
	if response.Code != http.StatusOK {
		t.Fatalf("runtime status = %d, want 200", response.Code)
	}
	var payload map[string]any
	if err := json.Unmarshal(response.Body.Bytes(), &payload); err != nil {
		t.Fatal(err)
	}
	if value, ok := payload["output_guard_ready"].(bool); !ok || value != want {
		t.Fatalf("output_guard_ready = %#v, proxy reports %v", payload["output_guard_ready"], want)
	}
	if payload["status"] != runtimeStatusError {
		t.Fatalf("readiness observation changed existing dependency status: %v", payload["status"])
	}
	if bytes.Contains(response.Body.Bytes(), []byte("runtime-status-test-secret")) {
		t.Fatal("runtime status exposed the admin secret")
	}
}
