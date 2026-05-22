package gateway

import (
	"context"
	"encoding/json"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/registry"
	"github.com/chiwei-platform/api-gateway/internal/route"
)

// snapProvider is a test stand-in for the loader: returns whatever snapshot is set.
type snapProvider struct {
	snap atomic.Pointer[route.Snapshot]
}

func (p *snapProvider) Current() *route.Snapshot { return p.snap.Load() }
func (p *snapProvider) set(s *route.Snapshot)    { p.snap.Store(s) }

// mockRegistry builds a registry client backed by the given services map.
func mockRegistry(t *testing.T, services map[string]registry.ServiceInfo) *registry.Client {
	t.Helper()
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(map[string]any{"services": services})
	}))
	t.Cleanup(srv.Close)
	reg := registry.NewClient(srv.URL, 1*time.Hour)
	time.Sleep(50 * time.Millisecond)
	return reg
}

func rule(name, prefix, reqLane string, t route.Target, fb string) route.Rule {
	return route.Rule{
		Name:     name,
		Enabled:  true,
		Priority: 100,
		Match:    route.Match{PathPrefix: prefix, RequestLane: reqLane},
		Targets:  []route.Target{t},
		Fallback: route.Fallback{Mode: fb},
	}
}

func TestGatewayColdStartEmergencyRoutes(t *testing.T) {
	reg := mockRegistry(t, nil)
	p := &snapProvider{} // current == nil -> cold start
	gw := New(p, reg, 5*time.Second)

	// Emergency-covered prefixes should NOT 503 from the matcher (they reach proxy and 502 on dead upstream).
	emergency := []string{"/api/paas/apps/", "/dashboard/index.html", "/dashboard/api/metrics"}
	for _, path := range emergency {
		req := httptest.NewRequest("GET", path, nil)
		w := httptest.NewRecorder()
		gw.ServeHTTP(w, req)
		if w.Code == http.StatusServiceUnavailable {
			t.Errorf("emergency path %q should route, got 503", path)
		}
	}

	// Non-emergency business paths must 503 on cold start.
	business := []string{"/webhook/x", "/api/agent/health", "/api/lark/x"}
	for _, path := range business {
		req := httptest.NewRequest("GET", path, nil)
		w := httptest.NewRecorder()
		gw.ServeHTTP(w, req)
		if w.Code != http.StatusServiceUnavailable {
			t.Errorf("business path %q on cold start: got %d want 503", path, w.Code)
		}
	}
}

func TestGatewayNoMatch404(t *testing.T) {
	reg := mockRegistry(t, nil)
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("paas", "/api/paas/", "", route.Target{Service: "paas-engine", Port: 8080}, "prod"),
	}))
	gw := New(p, reg, 5*time.Second)

	req := httptest.NewRequest("GET", "/totally/unknown", nil)
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)
	if w.Code != http.StatusNotFound {
		t.Errorf("non-matching with non-nil snapshot: got %d want 404", w.Code)
	}
}

// resolveTarget is the lane-resolution unit under test, isolated from proxying.
func TestResolveTargetLanePassthrough(t *testing.T) {
	reg := mockRegistry(t, map[string]registry.ServiceInfo{
		"agent-service": {Lanes: []string{"prod", "ppe-x"}, Port: 8000},
	})
	p := &snapProvider{}
	gw := New(p, reg, 5*time.Second)

	// target.lane empty -> follow request_lane
	tg := route.Target{Service: "agent-service", Port: 8000}

	host, _, status := gw.resolveTarget(tg, route.Fallback{Mode: "prod"}, "ppe-x")
	if status != 0 || host != "agent-service-ppe-x" {
		t.Errorf("passthrough ppe-x: host=%q status=%d", host, status)
	}

	host, _, status = gw.resolveTarget(tg, route.Fallback{Mode: "prod"}, "prod")
	if status != 0 || host != "agent-service" {
		t.Errorf("passthrough prod: host=%q status=%d", host, status)
	}

	host, _, status = gw.resolveTarget(tg, route.Fallback{Mode: "prod"}, "")
	if status != 0 || host != "agent-service" {
		t.Errorf("passthrough empty: host=%q status=%d", host, status)
	}
}

func TestResolveTargetLaneForced(t *testing.T) {
	reg := mockRegistry(t, map[string]registry.ServiceInfo{
		"agent-service": {Lanes: []string{"prod", "ppe-x"}, Port: 8000},
	})
	p := &snapProvider{}
	gw := New(p, reg, 5*time.Second)

	// target.lane non-empty -> force that lane, ignore request_lane
	tg := route.Target{Service: "agent-service", Lane: "ppe-x", Port: 8000}
	host, _, status := gw.resolveTarget(tg, route.Fallback{Mode: "prod"}, "prod")
	if status != 0 || host != "agent-service-ppe-x" {
		t.Errorf("forced ppe-x ignoring request prod: host=%q status=%d", host, status)
	}
}

func TestResolveTargetFallbackReject(t *testing.T) {
	reg := mockRegistry(t, map[string]registry.ServiceInfo{
		"agent-service": {Lanes: []string{"prod"}, Port: 8000}, // ppe-missing not present
	})
	p := &snapProvider{}
	gw := New(p, reg, 5*time.Second)

	// forced lane not in registry, fallback=reject -> status 503
	tg := route.Target{Service: "agent-service", Lane: "ppe-missing", Port: 8000}
	_, _, status := gw.resolveTarget(tg, route.Fallback{Mode: route.FallbackReject}, "prod")
	if status != http.StatusServiceUnavailable {
		t.Errorf("reject: expected 503, got status=%d", status)
	}
}

func TestResolveTargetFallbackProd(t *testing.T) {
	reg := mockRegistry(t, map[string]registry.ServiceInfo{
		"agent-service": {Lanes: []string{"prod"}, Port: 8000},
	})
	p := &snapProvider{}
	gw := New(p, reg, 5*time.Second)

	// forced lane not in registry, fallback=prod -> resolve to service prod
	tg := route.Target{Service: "agent-service", Lane: "ppe-missing", Port: 8000}
	host, _, status := gw.resolveTarget(tg, route.Fallback{Mode: route.FallbackProd}, "prod")
	if status != 0 || host != "agent-service" {
		t.Errorf("prod fallback: host=%q status=%d", host, status)
	}
}

func TestGatewayRedirectTrailingSlash(t *testing.T) {
	reg := mockRegistry(t, nil)
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("webhook", "/webhook/", "", route.Target{Service: "channel-proxy", Port: 3003}, "prod"),
	}))
	gw := New(p, reg, 5*time.Second)

	req := httptest.NewRequest("GET", "/webhook?foo=bar", nil)
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)
	if w.Code != http.StatusMovedPermanently {
		t.Fatalf("expected 301, got %d", w.Code)
	}
	if loc := w.Header().Get("Location"); loc != "/webhook/?foo=bar" {
		t.Errorf("Location: got %q want /webhook/?foo=bar", loc)
	}
}

func TestGatewayInjectsCtxLane(t *testing.T) {
	var gotCtxLane string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCtxLane = r.Header.Get("X-Ctx-Lane")
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return net.Dial(network, upstreamURL.Host)
		},
	}

	reg := mockRegistry(t, nil)
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("paas", "/api/paas/", "", route.Target{Service: "paas-engine", Port: 8080}, "prod"),
	}))
	gw := New(p, reg, 5*time.Second)
	gw.transport = transport

	req := httptest.NewRequest("GET", "/api/paas/apps/", nil)
	req.Header.Set("x-lane", "ppe-x")
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	if gotCtxLane != "ppe-x" {
		t.Errorf("X-Ctx-Lane: got %q want ppe-x", gotCtxLane)
	}
}

func TestGatewayStripPrefixForwarded(t *testing.T) {
	var gotPath string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotPath = r.URL.Path
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()

	upstreamURL, _ := url.Parse(upstream.URL)
	transport := &http.Transport{
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			return net.Dial(network, upstreamURL.Host)
		},
	}

	reg := mockRegistry(t, nil)
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("agent", "/api/agent/", "", route.Target{Service: "agent-service", Port: 8000, StripPrefix: "/api/agent"}, "prod"),
	}))
	gw := New(p, reg, 5*time.Second)
	gw.transport = transport

	req := httptest.NewRequest("GET", "/api/agent/health", nil)
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("expected 200, got %d", w.Code)
	}
	if gotPath != "/health" {
		t.Errorf("strip_prefix: upstream got path %q want /health", gotPath)
	}
}
