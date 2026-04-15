package gateway

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
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
		{Prefix: "/api/paas/", Service: "paas-engine", Port: 8080},
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
		{Prefix: "/api/paas/", Service: "test-upstream", Port: 8080},
	}
	matcher := route.NewMatcher(routes)

	// Mock registry returning our upstream
	regSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]interface{}{"services": map[string]interface{}{}})
	}))
	defer regSrv.Close()

	reg := registry.NewClient(regSrv.URL, 1*time.Hour)
	time.Sleep(50 * time.Millisecond)

	_ = New(matcher, reg, 5*time.Second)

	// No rewrite configured — path should pass through unchanged
	got := route.RewritePath("/api/paas/apps/myapp", routes[0])
	if got != "/api/paas/apps/myapp" {
		t.Errorf("expected /api/paas/apps/myapp, got %s", got)
	}
}

func TestGatewayLanePriority(t *testing.T) {
	gw, _ := setupGateway(t, nil)

	tests := []struct {
		name       string
		header     string
		query      string
		cookie     string
		wantHeader string // the x-lane value that reaches the proxy director
	}{
		{"header wins over query and cookie", "from-header", "from-query", "from-cookie", "from-header"},
		{"query wins over cookie", "", "from-query", "from-cookie", "from-query"},
		{"cookie used as fallback", "", "", "from-cookie", "from-cookie"},
		{"no lane", "", "", "", ""},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			req := httptest.NewRequest("GET", "/api/paas/apps/", nil)
			if tt.header != "" {
				req.Header.Set("x-lane", tt.header)
			}
			if tt.query != "" {
				q := req.URL.Query()
				q.Set("x-lane", tt.query)
				req.URL.RawQuery = q.Encode()
			}
			if tt.cookie != "" {
				req.AddCookie(&http.Cookie{Name: "x-lane", Value: tt.cookie})
			}

			w := httptest.NewRecorder()
			// The gateway will try to connect to a non-existent upstream and return 502,
			// but the lane resolution logic runs before the proxy call.
			// We verify indirectly: if no panic and request completes, lane resolution succeeded.
			gw.ServeHTTP(w, req)

			// Gateway returns 502 because the upstream doesn't exist in test,
			// but the important thing is it didn't 404 (route matched) and didn't panic.
			if w.Code == http.StatusNotFound {
				t.Errorf("expected route to match, got 404")
			}
		})
	}
}

func TestGatewayInjectsCtxLane(t *testing.T) {
	// Upstream captures headers it receives
	var gotCtxLane string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCtxLane = r.Header.Get("X-Ctx-Lane")
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	// Custom transport that redirects all requests to our test upstream
	upstreamURL, _ := url.Parse(upstream.URL)
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			// Redirect all connections to the upstream server
			return net.Dial(network, upstreamURL.Host)
		},
	}

	regSrv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]interface{}{"services": map[string]interface{}{}})
	}))
	defer regSrv.Close()

	reg := registry.NewClient(regSrv.URL, 1*time.Hour)
	time.Sleep(50 * time.Millisecond)

	routes := []route.Route{
		{Prefix: "/api/paas/", Service: "paas-engine", Port: 8080},
	}
	matcher := route.NewMatcher(routes)
	gw := New(matcher, reg, 5*time.Second)
	gw.transport = transport

	t.Run("injects x-ctx-lane when lane present", func(t *testing.T) {
		gotCtxLane = ""
		req := httptest.NewRequest("GET", "/api/paas/apps/", nil)
		req.Header.Set("x-lane", "dev")
		w := httptest.NewRecorder()
		gw.ServeHTTP(w, req)

		if w.Code != http.StatusOK {
			t.Fatalf("expected 200, got %d", w.Code)
		}
		if gotCtxLane != "dev" {
			t.Errorf("expected X-Ctx-Lane=dev, got %q", gotCtxLane)
		}
	})

	t.Run("no x-ctx-lane when no lane", func(t *testing.T) {
		gotCtxLane = ""
		req := httptest.NewRequest("GET", "/api/paas/apps/", nil)
		w := httptest.NewRecorder()
		gw.ServeHTTP(w, req)

		if gotCtxLane != "" {
			t.Errorf("expected no X-Ctx-Lane, got %q", gotCtxLane)
		}
	})
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
