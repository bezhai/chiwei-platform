package gateway

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync/atomic"
	"testing"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/route"
)

// snapProvider is a test stand-in for the loader: returns whatever snapshot is set.
type snapProvider struct {
	snap atomic.Pointer[route.Snapshot]
}

func (p *snapProvider) Current() *route.Snapshot { return p.snap.Load() }
func (p *snapProvider) set(s *route.Snapshot)    { p.snap.Store(s) }

func rule(name, prefix, reqLane string, t route.Target) route.Rule {
	if t.Weight == 0 {
		t.Weight = 100
	}
	return route.Rule{
		Name:     name,
		Enabled:  true,
		Priority: 100,
		Match:    route.Match{PathPrefix: prefix, RequestLane: reqLane},
		Targets:  []route.Target{t},
	}
}

func TestGatewayColdStartEmergencyRoutes(t *testing.T) {
	p := &snapProvider{} // current == nil -> cold start
	gw := New(p, 5*time.Second)

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
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("paas", "/api/paas/", "", route.Target{Service: "paas-engine", Port: 8080}),
	}))
	gw := New(p, 5*time.Second)

	req := httptest.NewRequest("GET", "/totally/unknown", nil)
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)
	if w.Code != http.StatusNotFound {
		t.Errorf("non-matching with non-nil snapshot: got %d want 404", w.Code)
	}
}

// TestForwardEffectiveLane locks the post-migration forwarding contract: the
// gateway always dials the logical service name and writes X-Ctx-Lane =
// effective_lane (target.Lane overrides request lane; empty -> header omitted).
// Lane resolution is the sidecar's job, so the gateway never rewrites the host
// to "service-lane" and never 503s on a missing lane.
func TestForwardEffectiveLane(t *testing.T) {
	var gotCtxLane string
	var ctxLanePresent bool
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCtxLane = r.Header.Get("X-Ctx-Lane")
		_, ctxLanePresent = r.Header["X-Ctx-Lane"]
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	upstreamURL, _ := url.Parse(upstream.URL)

	var dialedAddr string
	transport := &http.Transport{
		// Disable keep-alive so every case dials afresh (all cases dial the same
		// logical host, so a pooled connection would hide the dialed address).
		DisableKeepAlives: true,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			dialedAddr = addr
			return net.Dial(network, upstreamURL.Host)
		},
	}

	cases := []struct {
		name        string
		requestLane string
		targetLane  string
		wantCtxLane string
		wantCtxSet  bool
	}{
		{"default prod omits header", "", "", "", false},
		{"passthrough request lane", "ppe-a", "", "ppe-a", true},
		{"force target lane", "", "ppe-a", "ppe-a", true},
		{"target lane overrides request", "ppe-a", "ppe-b", "ppe-b", true},
		{"force prod", "ppe-a", "prod", "prod", true},
		{"absent target lane passes through missing request lane", "", "ppe-missing", "ppe-missing", true},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dialedAddr, gotCtxLane, ctxLanePresent = "", "", false

			p := &snapProvider{}
			p.set(route.NewSnapshot(1, []route.Rule{
				rule("agent", "/api/agent/", "", route.Target{Service: "agent-service", Lane: tc.targetLane, Port: 8000}),
			}))
			gw := New(p, 5*time.Second)
			gw.transport = transport

			req := httptest.NewRequest("GET", "/api/agent/health", nil)
			if tc.requestLane != "" {
				req.Header.Set("x-lane", tc.requestLane)
			}
			w := httptest.NewRecorder()
			gw.ServeHTTP(w, req)

			if w.Code != http.StatusOK {
				t.Fatalf("expected 200 (reached upstream), got %d", w.Code)
			}
			if dialedAddr != "agent-service:8000" {
				t.Errorf("dialed host: got %q want agent-service:8000 (must be logical name)", dialedAddr)
			}
			if ctxLanePresent != tc.wantCtxSet {
				t.Errorf("X-Ctx-Lane present=%v want %v (value=%q)", ctxLanePresent, tc.wantCtxSet, gotCtxLane)
			}
			if tc.wantCtxSet && gotCtxLane != tc.wantCtxLane {
				t.Errorf("X-Ctx-Lane: got %q want %q", gotCtxLane, tc.wantCtxLane)
			}
		})
	}
}

// TestColdStartForwardEffectiveLane verifies the same forwarding contract holds
// on the cold-start (nil snapshot -> EmergencyRules) path: the gateway dials the
// logical service and propagates the request lane as X-Ctx-Lane.
func TestColdStartForwardEffectiveLane(t *testing.T) {
	var gotCtxLane string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCtxLane = r.Header.Get("X-Ctx-Lane")
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	upstreamURL, _ := url.Parse(upstream.URL)

	var dialedAddr string
	transport := &http.Transport{
		DisableKeepAlives: true,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			dialedAddr = addr
			return net.Dial(network, upstreamURL.Host)
		},
	}

	p := &snapProvider{} // current == nil -> cold start, EmergencyRules
	gw := New(p, 5*time.Second)
	gw.transport = transport

	req := httptest.NewRequest("GET", "/api/paas/apps/", nil)
	req.Header.Set("x-lane", "ppe-a")
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("cold-start emergency forward: expected 200, got %d", w.Code)
	}
	if dialedAddr != "paas-engine:8080" {
		t.Errorf("dialed host: got %q want paas-engine:8080 (logical name)", dialedAddr)
	}
	if gotCtxLane != "ppe-a" {
		t.Errorf("X-Ctx-Lane: got %q want ppe-a", gotCtxLane)
	}
}

func TestGatewayRedirectTrailingSlash(t *testing.T) {
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("webhook", "/webhook/", "", route.Target{Service: "channel-proxy", Port: 3003}),
	}))
	gw := New(p, 5*time.Second)

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

	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("paas", "/api/paas/", "", route.Target{Service: "paas-engine", Port: 8080}),
	}))
	gw := New(p, 5*time.Second)
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

// TestForwardWeightedRandomPicksTarget locks the end-to-end forwarding for a
// multi-target rule: with the random source injected to a fixed draw, the
// gateway dials the chosen target's service and writes its lane into
// X-Ctx-Lane. draw=0.0 -> 90-weight target A (lane prod), draw=0.95 -> 10-weight
// target B (lane ppe-new). Deterministic, not flaky.
func TestForwardWeightedRandomPicksTarget(t *testing.T) {
	var gotCtxLane string
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		gotCtxLane = r.Header.Get("X-Ctx-Lane")
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	upstreamURL, _ := url.Parse(upstream.URL)

	var dialedAddr string
	transport := &http.Transport{
		DisableKeepAlives: true,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			dialedAddr = addr
			return net.Dial(network, upstreamURL.Host)
		},
	}

	multiRule := route.Rule{
		Name:     "agent",
		Enabled:  true,
		Priority: 100,
		Match:    route.Match{PathPrefix: "/api/agent/"},
		Targets: []route.Target{
			{Service: "agent-a", Lane: "prod", Port: 8000, Weight: 90},
			{Service: "agent-b", Lane: "ppe-new", Port: 8000, Weight: 10},
		},
	}

	cases := []struct {
		name        string
		draw        float64
		wantService string
		wantCtxLane string
	}{
		{"draw below boundary picks A", 0.0, "agent-a", "prod"},
		{"draw at boundary picks B", 0.90, "agent-b", "ppe-new"},
		{"draw in B range picks B", 0.95, "agent-b", "ppe-new"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dialedAddr, gotCtxLane = "", ""
			p := &snapProvider{}
			p.set(route.NewSnapshot(1, []route.Rule{multiRule}))
			gw := New(p, 5*time.Second)
			gw.transport = transport
			gw.rng = func() float64 { return tc.draw }

			req := httptest.NewRequest("GET", "/api/agent/health", nil)
			w := httptest.NewRecorder()
			gw.ServeHTTP(w, req)

			if w.Code != http.StatusOK {
				t.Fatalf("expected 200, got %d", w.Code)
			}
			wantAddr := tc.wantService + ":8000"
			if dialedAddr != wantAddr {
				t.Errorf("dialed: got %q want %q", dialedAddr, wantAddr)
			}
			if gotCtxLane != tc.wantCtxLane {
				t.Errorf("X-Ctx-Lane: got %q want %q", gotCtxLane, tc.wantCtxLane)
			}
		})
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

	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{
		rule("agent", "/api/agent/", "", route.Target{Service: "agent-service", Port: 8000, StripPrefix: "/api/agent"}),
	}))
	gw := New(p, 5*time.Second)
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
