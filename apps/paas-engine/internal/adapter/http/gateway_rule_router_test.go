package http

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/service"
)

const testAPIToken = "test-token"

// buildFullRouter wires the production NewRouter with only the gateway-rule
// handler populated (others nil — unused in these tests) plus auth enabled.
func buildFullGatewayRouter() http.Handler {
	svc := service.NewGatewayRuleService(newGwStubRepo())
	gwH := NewGatewayRuleHandler(svc)
	return NewRouter(nil, nil, nil, nil, nil, nil, nil, nil, gwH, testAPIToken)
}

func reqWithAuth(t *testing.T, r http.Handler, method, path, body, token string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, bytes.NewReader([]byte(body)))
	if token != "" {
		req.Header.Set("X-API-Key", token)
	}
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)
	return rec
}

func TestGatewayRouter_ManagementRequiresAuth(t *testing.T) {
	r := buildFullGatewayRouter()
	rec := reqWithAuth(t, r, http.MethodGet, "/api/paas/gateway-rules", "", "")
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 without token, got %d", rec.Code)
	}
}

func TestGatewayRouter_FullLifecycleWithAuth(t *testing.T) {
	r := buildFullGatewayRouter()

	rec := reqWithAuth(t, r, http.MethodPut, "/api/paas/gateway-rules/default-agent-service-api", validRuleBody(), testAPIToken)
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	rec = reqWithAuth(t, r, http.MethodGet, "/api/paas/gateway-rules/default-agent-service-api", "", testAPIToken)
	if rec.Code != http.StatusOK {
		t.Fatalf("GET single expected 200, got %d", rec.Code)
	}

	rec = reqWithAuth(t, r, http.MethodDelete, "/api/paas/gateway-rules/default-agent-service-api", "", testAPIToken)
	if rec.Code != http.StatusOK {
		t.Fatalf("DELETE expected 200, got %d", rec.Code)
	}
}

func TestGatewayRouter_InternalSnapshotNoAuth(t *testing.T) {
	r := buildFullGatewayRouter()

	// seed via authed PUT
	if rec := reqWithAuth(t, r, http.MethodPut, "/api/paas/gateway-rules/default-agent-service-api", validRuleBody(), testAPIToken); rec.Code != http.StatusOK {
		t.Fatalf("seed PUT failed: %d %s", rec.Code, rec.Body.String())
	}

	// internal endpoint must work WITHOUT a token
	rec := reqWithAuth(t, r, http.MethodGet, "/internal/gateway-rules", "", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("internal snapshot expected 200 without auth, got %d", rec.Code)
	}
	var snap struct {
		Version   int64                `json:"version"`
		UpdatedAt string               `json:"updated_at"`
		Rules     []domain.GatewayRule `json:"rules"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v", err)
	}
	if len(snap.Rules) != 1 || snap.Version != 1 {
		t.Fatalf("unexpected snapshot: version=%d rules=%d", snap.Version, len(snap.Rules))
	}
}
