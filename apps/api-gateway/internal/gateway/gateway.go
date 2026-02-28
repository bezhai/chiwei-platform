package gateway

import (
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/registry"
	"github.com/chiwei-platform/api-gateway/internal/route"
)

// Gateway is the core HTTP handler that routes requests based on path prefix
// and x-lane header, then proxies to the appropriate upstream service.
type Gateway struct {
	matcher  *route.Matcher
	registry *registry.Client
	timeout  time.Duration
}

// New creates a Gateway with the given matcher, registry client, and proxy timeout.
func New(matcher *route.Matcher, reg *registry.Client, timeout time.Duration) *Gateway {
	return &Gateway{
		matcher:  matcher,
		registry: reg,
		timeout:  timeout,
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

	// Determine lane from header
	lane := r.Header.Get("x-lane")

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
	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL = target
			req.Host = target.Host
			// Preserve original headers including x-lane
			if _, ok := req.Header["User-Agent"]; !ok {
				req.Header.Set("User-Agent", "")
			}
		},
		Transport: &http.Transport{
			ResponseHeaderTimeout: g.timeout,
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			slog.Error("proxy error",
				"service", rt.Service,
				"target", target.String(),
				"error", err,
			)
			http.Error(w, fmt.Sprintf("bad gateway: %s", err), http.StatusBadGateway)
		},
	}

	proxy.ServeHTTP(w, r)
}
