package gateway

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/registry"
	"github.com/chiwei-platform/api-gateway/internal/route"
)

func setupGateway(t *testing.T, upstream *httptest.Server) (*Gateway, *registry.Client) {
	t.Helper()

	// Mock registry that returns the upstream's host info
	regSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// Return empty services - gateway will use default port
		json.NewEncoder(w).Encode(map[string]interface{}{"services": map[string]interface{}{}})
	}))
	t.Cleanup(regSrv.Close)

	reg := registry.NewClient(regSrv.URL, 1*time.Hour)
	time.Sleep(50 * time.Millisecond)

	routes := []route.Route{
		{Prefix: "/api/paas/", Service: "paas-engine", Port: 8080, StripPrefix: "/api/paas", RewritePrefix: "/api/v1"},
		{Prefix: "/webhook/", Service: "lark-proxy", Port: 3003},
	}
	matcher := route.NewMatcher(routes)

	gw := New(matcher, reg, 5*time.Second)
	return gw, reg
}

func TestGatewayNotFound(t *testing.T) {
	gw, _ := setupGateway(t, nil)

	req := httptest.NewRequest("GET", "/unknown/path", nil)
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)

	if w.Code != http.StatusNotFound {
		t.Errorf("expected 404, got %d", w.Code)
	}
}

func TestGatewayPathRewrite(t *testing.T) {
	// Upstream that echoes the request path
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		io.WriteString(w, "ok")
	}))
	defer upstream.Close()

	// Create a gateway that points to our upstream
	routes := []route.Route{
		{Prefix: "/api/paas/", Service: "test-upstream", Port: 8080, StripPrefix: "/api/paas", RewritePrefix: "/api/v1"},
	}
	matcher := route.NewMatcher(routes)

	// Mock registry returning our upstream
	regSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]interface{}{"services": map[string]interface{}{}})
	}))
	defer regSrv.Close()

	reg := registry.NewClient(regSrv.URL, 1*time.Hour)
	time.Sleep(50 * time.Millisecond)

	// Override: we need to point to the actual upstream server
	// Since registry won't know about "test-upstream", the gateway will try to connect to test-upstream:8080
	// For unit testing, we test the path rewriting logic through the matcher directly
	_ = New(matcher, reg, 5*time.Second)

	// Test path rewriting directly
	got := route.RewritePath("/api/paas/apps/myapp", routes[0])
	if got != "/api/v1/apps/myapp" {
		t.Errorf("expected /api/v1/apps/myapp, got %s", got)
	}
}

func TestGatewayRedirectTrailingSlash(t *testing.T) {
	gw, _ := setupGateway(t, nil)

	req := httptest.NewRequest("GET", "/webhook?foo=bar", nil)
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)

	if w.Code != http.StatusMovedPermanently {
		t.Errorf("expected 301, got %d", w.Code)
	}
	loc := w.Header().Get("Location")
	if loc != "/webhook/?foo=bar" {
		t.Errorf("expected /webhook/?foo=bar, got %s", loc)
	}
}
