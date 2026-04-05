// Package proxy provides a transparent HTTP reverse proxy that intercepts
// outbound traffic (via iptables REDIRECT) and routes requests to lane-specific
// service instances based on the x-ctx-lane header.
package proxy

import (
	"context"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/registry"
)

// HostMapper translates a logical host (e.g. "agent-service-dev:8000") to an
// actual network address. In production this is the identity function; in
// tests it redirects to httptest servers.
type HostMapper func(host string) string

// DefaultHostMapper returns the host unchanged — used in production where
// DNS resolution handles the mapping.
func DefaultHostMapper(host string) string { return host }

// Handler is an http.Handler that reverse-proxies every incoming request
// after resolving the target through the lane registry.
type Handler struct {
	resolver   registry.Resolver
	hostMapper HostMapper
}

// NewHandler creates a Handler. If hostMapper is nil, DefaultHostMapper is used.
func NewHandler(resolver registry.Resolver, hostMapper HostMapper) *Handler {
	if hostMapper == nil {
		hostMapper = DefaultHostMapper
	}
	return &Handler{resolver: resolver, hostMapper: hostMapper}
}

// ServeHTTP resolves the request's target host via the lane registry,
// applies the host mapper, and reverse-proxies the request.
func (h *Handler) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	lane := r.Header.Get("x-ctx-lane")
	targetHost := h.resolver.ResolveHost(r.Host, lane)
	actualHost := h.hostMapper(targetHost)

	target := &url.URL{
		Scheme: "http",
		Host:   actualHost,
	}

	proxy := &httputil.ReverseProxy{
		Director: func(req *http.Request) {
			req.URL.Scheme = target.Scheme
			req.URL.Host = target.Host
			// Preserve the original Host header so upstream services
			// see the logical service name, not the resolved address.
			req.Host = r.Host
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("[proxy] error forwarding to %s: %v", actualHost, err)
			http.Error(w, "sidecar proxy error", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(w, r)
}

// Server wraps a Handler in an http.Server with production-ready timeouts.
type Server struct {
	handler    *Handler
	listenAddr string
	httpServer *http.Server
}

// NewServer creates a Server that listens on listenAddr (e.g. ":15001")
// and routes traffic through the given resolver.
func NewServer(listenAddr string, resolver registry.Resolver) *Server {
	handler := NewHandler(resolver, nil)
	s := &Server{
		handler:    handler,
		listenAddr: listenAddr,
	}
	s.httpServer = &http.Server{
		Handler:      handler,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}
	return s
}

// ListenAndServe starts the proxy server. It blocks until the server is
// shut down or encounters a fatal error.
func (s *Server) ListenAndServe() error {
	ln, err := net.Listen("tcp", s.listenAddr)
	if err != nil {
		return err
	}
	log.Printf("[proxy] listening on %s", s.listenAddr)
	return s.httpServer.Serve(ln)
}

// Shutdown gracefully shuts down the proxy server.
func (s *Server) Shutdown(ctx context.Context) error {
	return s.httpServer.Shutdown(ctx)
}
