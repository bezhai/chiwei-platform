package gateway

import (
	"fmt"
	"hash/fnv"
	"log/slog"
	"math/rand"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/middleware"
	"github.com/chiwei-platform/api-gateway/internal/route"
)

// SnapshotProvider supplies the current routing snapshot. It is nil at cold
// start (no snapshot ever fetched).
type SnapshotProvider interface {
	Current() *route.Snapshot
}

// Gateway routes requests using the dynamic snapshot (three-layer fallback) and
// the x-lane header, then proxies to the resolved upstream.
type Gateway struct {
	snapshots SnapshotProvider
	timeout   time.Duration
	transport *http.Transport
	// rng returns a value in [0,1) used for weighted-random target selection.
	// Injectable so tests can drive deterministic target choices.
	rng func() float64
	// hash maps the (rule_name + split key) string to a uint64 for stable
	// (sticky) target selection. Injectable so tests can pin a bucket.
	hash hashFunc
}

// New creates a Gateway.
func New(snapshots SnapshotProvider, timeout time.Duration) *Gateway {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConns = 100
	t.MaxIdleConnsPerHost = 100
	t.ResponseHeaderTimeout = timeout

	return &Gateway{
		snapshots: snapshots,
		timeout:   timeout,
		transport: t,
		rng:       rand.Float64,
		hash:      fnvHash,
	}
}

// fnvHash is the production stable-split hash: FNV-1a over the input bytes.
func fnvHash(s string) uint64 {
	h := fnv.New64a()
	_, _ = h.Write([]byte(s))
	return h.Sum64()
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Resolve request lane: header > query > cookie (existing passthrough order).
	requestLane := r.Header.Get("x-lane")
	if requestLane == "" {
		requestLane = r.URL.Query().Get("x-lane")
	}
	if requestLane == "" {
		if c, err := r.Cookie("x-lane"); err == nil {
			requestLane = c.Value
		}
	}
	// Persist lane via cookie when it arrived as a query param so subsequent
	// browser requests (static assets) carry it automatically.
	if requestLane != "" && r.Header.Get("x-lane") == "" {
		http.SetCookie(w, &http.Cookie{Name: "x-lane", Value: requestLane, Path: "/"})
	}

	// Three-layer fallback: nil snapshot (cold start) -> emergency rules; else
	// dynamic snapshot.
	snap := g.snapshots.Current()
	if snap == nil {
		g.serveEmergency(w, r, requestLane)
		return
	}

	matcher := route.NewMatcher(snap)
	result, ok := matcher.Match(r.URL.Path, requestLane)
	if !ok {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if result.Redirect {
		redirectTrailingSlash(w, r)
		return
	}
	g.forward(w, r, result.Rule, requestLane)
}

// redirectTrailingSlash issues a 301 from "/foo" to "/foo/" preserving query.
func redirectTrailingSlash(w http.ResponseWriter, r *http.Request) {
	target := r.URL.Path + "/"
	if r.URL.RawQuery != "" {
		target += "?" + r.URL.RawQuery
	}
	http.Redirect(w, r, target, http.StatusMovedPermanently)
}

// serveEmergency handles requests at cold start using the hardcoded life-saving
// rules. Anything not covered returns 503.
func (g *Gateway) serveEmergency(w http.ResponseWriter, r *http.Request, requestLane string) {
	// version 0 marks the hardcoded cold-start snapshot (real versions start at 1+).
	matcher := route.NewMatcher(route.NewSnapshot(0, route.EmergencyRules()))
	result, ok := matcher.Match(r.URL.Path, requestLane)
	if !ok {
		slog.Warn("cold start: no emergency route", "path", r.URL.Path)
		http.Error(w, "service unavailable", http.StatusServiceUnavailable)
		return
	}
	if result.Redirect {
		redirectTrailingSlash(w, r)
		return
	}
	g.forward(w, r, result.Rule, requestLane)
}

// forward proxies the request to the matched target's logical service. Lane
// resolution is delegated to the lane-sidecar via the X-Ctx-Lane header; the
// gateway never resolves "service-lane" itself.
// chooseTarget selects the upstream target for a matched rule. When the rule
// configures split_key_headers and a key resolves from the request, selection
// is stable (hash(rule+key) -> fixed bucket). Otherwise it falls back to
// weighted random; if the rule was configured for stable split but no key
// resolved, the fallback metric is bumped for the rule.
func (g *Gateway) chooseTarget(rt route.Rule, r *http.Request) route.Target {
	if len(rt.SplitKeyHeaders) > 0 {
		if key, ok := resolveSplitKey(rt.SplitKeyHeaders, r.Header); ok {
			bucket := stableBucket(g.hash, rt.Name, key)
			return selectTargetStable(rt.Targets, bucket)
		}
		middleware.GatewaySplitFallbackTotal.WithLabelValues(rt.Name).Inc()
	}
	return selectTarget(rt.Targets, g.rng())
}

func (g *Gateway) forward(w http.ResponseWriter, r *http.Request, rt route.Rule, requestLane string) {
	target := g.chooseTarget(rt, r)
	effLane := effectiveLane(target, requestLane)

	targetPath := route.RewritePath(r.URL.Path, target)
	upstreamURL := &url.URL{
		Scheme:   "http",
		Host:     fmt.Sprintf("%s:%d", target.Service, target.Port),
		Path:     targetPath,
		RawQuery: r.URL.RawQuery,
	}

	proxyStart := time.Now()
	pw := &proxyResponseWriter{ResponseWriter: w, status: http.StatusOK}

	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL = upstreamURL
			req.Host = upstreamURL.Host
			if effLane != "" {
				req.Header.Set("X-Ctx-Lane", effLane)
			} else {
				req.Header.Del("X-Ctx-Lane")
			}
			if _, ok := req.Header["User-Agent"]; !ok {
				req.Header.Set("User-Agent", "")
			}
		},
		Transport: g.transport,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			slog.Error("proxy error", "service", target.Service, "target", upstreamURL.String(), "error", err)
			http.Error(w, fmt.Sprintf("bad gateway: %s", err), http.StatusBadGateway)
		},
	}

	proxy.ServeHTTP(pw, r)

	middleware.ProxyRequestsTotal.WithLabelValues(target.Service, strconv.Itoa(pw.status)).Inc()
	middleware.ProxyDuration.WithLabelValues(target.Service).Observe(time.Since(proxyStart).Seconds())
}

// effectiveLane is the lane intent propagated downstream via X-Ctx-Lane:
// target.Lane overrides, otherwise the request lane passes through. Resolving
// that lane to a real pod (and any fail-closed behavior) is the sidecar's job.
func effectiveLane(t route.Target, requestLane string) string {
	if t.Lane != "" {
		return t.Lane
	}
	return requestLane
}

type proxyResponseWriter struct {
	http.ResponseWriter
	status int
}

func (w *proxyResponseWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}
