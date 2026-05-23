package gateway

import (
	"context"
	"net"
	"net/http"
	"net/http/httptest"
	"net/url"
	"testing"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/middleware"
	"github.com/chiwei-platform/api-gateway/internal/route"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// dialRecordingTransport returns a transport that records the dialed address
// and routes every connection to the given test upstream.
func dialRecordingTransport(upstreamHost string, dialed *string) *http.Transport {
	return &http.Transport{
		DisableKeepAlives: true,
		DialContext: func(ctx context.Context, network, addr string) (net.Conn, error) {
			*dialed = addr
			return net.Dial(network, upstreamHost)
		},
	}
}

func splitRule(headers []string) route.Rule {
	return route.Rule{
		Name:            "agent",
		Enabled:         true,
		Priority:        100,
		Match:           route.Match{PathPrefix: "/api/agent/"},
		SplitKeyHeaders: headers,
		Targets: []route.Target{
			{Service: "agent-a", Lane: "prod", Port: 8000, Weight: 90},
			{Service: "agent-b", Lane: "ppe-new", Port: 8000, Weight: 10},
		},
	}
}

// TestForwardStableSticky: with split_key_headers configured and a key present,
// the same key always reaches the same target regardless of the random source.
// We pin a hash that we know lands the key in B's bucket, and assert the choice
// is independent of g.rng (which would have picked A).
func TestForwardStableSticky(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	upstreamURL, _ := url.Parse(upstream.URL)

	var dialed string
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{splitRule([]string{"X-User-Id"})}))
	gw := New(p, 5*time.Second)
	gw.transport = dialRecordingTransport(upstreamURL.Host, &dialed)
	// rng would pick A (draw 0.0), but a stable key must override it.
	gw.rng = func() float64 { return 0.0 }
	// Hash forces bucket 95 -> within B's [90,100) range.
	gw.hash = func(string) uint64 { return 95 }

	for i := 0; i < 5; i++ {
		dialed = ""
		req := httptest.NewRequest("GET", "/api/agent/health", nil)
		req.Header.Set("X-User-Id", "stable-user")
		w := httptest.NewRecorder()
		gw.ServeHTTP(w, req)
		if w.Code != http.StatusOK {
			t.Fatalf("iter %d: expected 200, got %d", i, w.Code)
		}
		if dialed != "agent-b:8000" {
			t.Errorf("iter %d: stable key must always pick agent-b, dialed %q", i, dialed)
		}
	}
}

// TestForwardStableUsesHashNotRng: the bucket comes from hash(rule+key), not
// from g.rng. With hash forcing bucket 10 (-> A) the target is A even though
// rng=0.99 (which random selection would map to B).
func TestForwardStableUsesHashNotRng(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	upstreamURL, _ := url.Parse(upstream.URL)

	var dialed string
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{splitRule([]string{"X-User-Id"})}))
	gw := New(p, 5*time.Second)
	gw.transport = dialRecordingTransport(upstreamURL.Host, &dialed)
	gw.rng = func() float64 { return 0.99 } // random would pick B
	gw.hash = func(string) uint64 { return 10 } // stable bucket 10 -> A

	req := httptest.NewRequest("GET", "/api/agent/health", nil)
	req.Header.Set("X-User-Id", "u")
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)
	if dialed != "agent-a:8000" {
		t.Errorf("hash bucket 10 must pick agent-a, dialed %q", dialed)
	}
}

// TestForwardFallbackNoKeyRecordsMetric: split_key_headers configured but the
// header is absent -> fall back to weighted random (driven by rng) AND bump the
// fallback metric for this rule.
func TestForwardFallbackNoKeyRecordsMetric(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	upstreamURL, _ := url.Parse(upstream.URL)

	var dialed string
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{splitRule([]string{"X-User-Id"})}))
	gw := New(p, 5*time.Second)
	gw.transport = dialRecordingTransport(upstreamURL.Host, &dialed)
	gw.rng = func() float64 { return 0.0 } // random picks A
	gw.hash = func(string) uint64 { return 95 } // would pick B if key existed

	before := testutil.ToFloat64(middleware.GatewaySplitFallbackTotal.WithLabelValues("agent"))

	req := httptest.NewRequest("GET", "/api/agent/health", nil) // no X-User-Id
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)

	if dialed != "agent-a:8000" {
		t.Errorf("no key must fall back to weighted random (rng=0 -> A), dialed %q", dialed)
	}
	after := testutil.ToFloat64(middleware.GatewaySplitFallbackTotal.WithLabelValues("agent"))
	if after-before != 1 {
		t.Errorf("fallback metric for rule agent: delta=%v want 1", after-before)
	}
}

// TestForwardNoSplitHeadersNoFallbackMetric: a rule WITHOUT split_key_headers
// uses weighted random and does NOT bump the fallback metric (fallback metric
// only fires when a rule is configured for stable split but cannot resolve a
// key — not for plain unconfigured rules).
func TestForwardNoSplitHeadersNoFallbackMetric(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer upstream.Close()
	upstreamURL, _ := url.Parse(upstream.URL)

	var dialed string
	p := &snapProvider{}
	p.set(route.NewSnapshot(1, []route.Rule{splitRule(nil)})) // no split headers
	gw := New(p, 5*time.Second)
	gw.transport = dialRecordingTransport(upstreamURL.Host, &dialed)
	gw.rng = func() float64 { return 0.99 } // picks B

	before := testutil.ToFloat64(middleware.GatewaySplitFallbackTotal.WithLabelValues("agent"))

	req := httptest.NewRequest("GET", "/api/agent/health", nil)
	w := httptest.NewRecorder()
	gw.ServeHTTP(w, req)

	if dialed != "agent-b:8000" {
		t.Errorf("unconfigured rule uses weighted random (rng=0.99 -> B), dialed %q", dialed)
	}
	after := testutil.ToFloat64(middleware.GatewaySplitFallbackTotal.WithLabelValues("agent"))
	if after != before {
		t.Errorf("unconfigured rule must not bump fallback metric: delta=%v", after-before)
	}
}
