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

// Gateway is the core HTTP handler that routes requests based on path prefix
// and x-lane header, then proxies to the appropriate upstream service.
type Gateway struct {
	matcher   *route.Matcher
	registry  *registry.Client
	timeout   time.Duration
	transport *http.Transport
}

// New creates a Gateway with the given matcher, registry client, and proxy timeout.
func New(matcher *route.Matcher, reg *registry.Client, timeout time.Duration) *Gateway {
	t := http.DefaultTransport.(*http.Transport).Clone()
	t.MaxIdleConns = 100
	t.MaxIdleConnsPerHost = 100
	t.ResponseHeaderTimeout = timeout

	return &Gateway{
		matcher:   matcher,
		registry:  reg,
		timeout:   timeout,
		transport: t,
	}
}

func (g *Gateway) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	// Match route by path prefix
	result, ok := g.matcher.Match(r.URL.Path)
	if !ok {
		http.Error(w, "not found", http.StatusNotFound)
		return
	}
	if result.Redirect {
		target := r.URL.Path + "/"
		if r.URL.RawQuery != "" {
			target += "?" + r.URL.RawQuery
		}
		http.Redirect(w, r, target, http.StatusMovedPermanently)
		return
	}
	rt := result.Route

	// Determine lane: header > query param > cookie
	lane := r.Header.Get("x-lane")
	if lane == "" {
		lane = r.URL.Query().Get("x-lane")
	}
	if lane == "" {
		if c, err := r.Cookie("x-lane"); err == nil {
			lane = c.Value
		}
	}

	// Set cookie when lane comes from query param so subsequent
	// browser requests (static assets) carry the lane automatically.
	if lane != "" && r.Header.Get("x-lane") == "" {
		http.SetCookie(w, &http.Cookie{Name: "x-lane", Value: lane, Path: "/"})
	}

	// Resolve upstream host:port via registry (with fallback)
	host, port := g.registry.Resolve(rt.Service, lane, rt.Port)

	// Rewrite path
	targetPath := route.RewritePath(r.URL.Path, rt)

	// Build target URL
	target := &url.URL{
		Scheme:   "http",
		Host:     fmt.Sprintf("%s:%d", host, port),
		Path:     targetPath,
		RawQuery: r.URL.RawQuery,
	}

	// Create reverse proxy
	proxyStart := time.Now()
	pw := &proxyResponseWriter{ResponseWriter: w, status: http.StatusOK}

	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL = target
			req.Host = target.Host
			// Preserve original headers including x-lane
			if _, ok := req.Header["User-Agent"]; !ok {
				req.Header.Set("User-Agent", "")
			}
		},
		Transport: g.transport,
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			slog.Error("proxy error",
				"service", rt.Service,
				"target", target.String(),
				"error", err,
			)
			http.Error(w, fmt.Sprintf("bad gateway: %s", err), http.StatusBadGateway)
		},
	}

	proxy.ServeHTTP(pw, r)

	middleware.ProxyRequestsTotal.WithLabelValues(rt.Service, strconv.Itoa(pw.status)).Inc()
	middleware.ProxyDuration.WithLabelValues(rt.Service).Observe(time.Since(proxyStart).Seconds())
}

type proxyResponseWriter struct {
	http.ResponseWriter
	status int
}

func (w *proxyResponseWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}
