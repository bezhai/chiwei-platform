package gateway

import (
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"strconv"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/middleware"
	"github.com/chiwei-platform/api-gateway/internal/registry"
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
	registry  *registry.Client
	timeout   time.Duration
	transport *http.Transport
}

// New creates a Gateway.
func New(snapshots SnapshotProvider, reg *registry.Client, timeout time.Duration) *Gateway {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConns = 100
	t.MaxIdleConnsPerHost = 100
	t.ResponseHeaderTimeout = timeout

	return &Gateway{
		snapshots: snapshots,
		registry:  reg,
		timeout:   timeout,
		transport: t,
	}
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

// forward resolves the upstream for a matched rule and proxies the request.
func (g *Gateway) forward(w http.ResponseWriter, r *http.Request, rt route.Rule, requestLane string) {
	target := rt.Targets[0]

	host, port, status := g.resolveTarget(target, rt.Fallback, requestLane)
	if status != 0 {
		http.Error(w, "service unavailable", status)
		return
	}

	targetPath := route.RewritePath(r.URL.Path, target)
	upstreamURL := &url.URL{
		Scheme:   "http",
		Host:     fmt.Sprintf("%s:%d", host, port),
		Path:     targetPath,
		RawQuery: r.URL.RawQuery,
	}

	proxyStart := time.Now()
	pw := &proxyResponseWriter{ResponseWriter: w, status: http.StatusOK}

	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL = upstreamURL
			req.Host = upstreamURL.Host
			if requestLane != "" {
				req.Header.Set("X-Ctx-Lane", requestLane)
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

// resolveTarget computes the upstream host:port for a target.
//
// Lane selection: target.Lane empty -> follow requestLane (passthrough, current
// behavior); non-empty -> force that lane.
//
// Fallback: if the desired lane is non-empty and non-"prod" but the registry
// has no instance for it, registry.Resolve returns the bare service host
// (prod). We detect that "lane not found" condition and apply fallback.Mode:
// "prod" keeps the prod resolution, "reject" returns status 503. A returned
// status of 0 means "proceed with host:port".
func (g *Gateway) resolveTarget(t route.Target, fb route.Fallback, requestLane string) (host string, port int, status int) {
	desiredLane := t.Lane
	if desiredLane == "" {
		desiredLane = requestLane
	}

	host, port = g.registry.Resolve(t.Service, desiredLane, t.Port)

	// A non-empty, non-prod desired lane that did not produce a "{service}-{lane}"
	// host means the lane has no instance in the registry -> fallback applies.
	laneRequested := desiredLane != "" && desiredLane != "prod"
	laneResolved := host == fmt.Sprintf("%s-%s", t.Service, desiredLane)
	if laneRequested && !laneResolved {
		if fb.Mode == route.FallbackReject {
			slog.Warn("target lane not in registry, rejecting",
				"service", t.Service, "lane", desiredLane)
			return "", 0, http.StatusServiceUnavailable
		}
		// FallbackProd (or default): keep prod resolution (host already == service).
	}
	return host, port, 0
}

type proxyResponseWriter struct {
	http.ResponseWriter
	status int
}

func (w *proxyResponseWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}
