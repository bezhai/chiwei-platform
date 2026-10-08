// Package proxy provides a transparent reverse proxy that intercepts outbound
// traffic (via iptables REDIRECT) and routes HTTP requests to lane-specific
// service instances based on the x-ctx-lane header. Non-HTTP traffic is
// passed through to the original destination via TCP tunneling.
package proxy

import (
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sync"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/registry"
	"github.com/soheilhy/cmux"
)

// HostMapper translates a logical host (e.g. "agent-service-dev:8000") to an
// actual network address. In production this is the identity function; in
// tests it redirects to httptest servers.
type HostMapper func(host string) string

// DefaultHostMapper returns the host unchanged.
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
			req.Host = r.Host
		},
		ErrorHandler: func(w http.ResponseWriter, r *http.Request, err error) {
			log.Printf("[proxy] error forwarding to %s: %v", actualHost, err)
			http.Error(w, "sidecar proxy error", http.StatusBadGateway)
		},
	}
	proxy.ServeHTTP(w, r)
}

// ErrServerClosed is returned by Serve and ListenAndServe once Shutdown has
// begun.
var ErrServerClosed = errors.New("proxy: server closed")

// Server uses cmux to multiplex a single TCP listener into HTTP and non-HTTP
// streams. HTTP traffic gets lane-aware routing; everything else gets TCP
// passthrough to the original destination (via SO_ORIGINAL_DST).
type Server struct {
	handler    *Handler
	listenAddr string
	httpServer *http.Server
	// originalDst finds where a passthrough connection was headed. It is
	// GetOriginalDst in production; tests point it at a local upstream,
	// since SO_ORIGINAL_DST only answers behind an iptables REDIRECT.
	originalDst func(*net.TCPConn) (net.Addr, error)

	mu       sync.Mutex
	listener net.Listener
	mux      cmux.CMux
	closing  bool                 // Shutdown has begun: no new tunnels
	draining chan struct{}        // closed when closing becomes true
	tunnels  map[*tunnel]struct{} // every open tunnel
	open     sync.WaitGroup       // one count per entry in tunnels
}

// NewServer creates a Server that listens on listenAddr (e.g. ":15001").
func NewServer(listenAddr string, resolver registry.Resolver) *Server {
	handler := NewHandler(resolver, nil)
	s := &Server{
		handler:     handler,
		listenAddr:  listenAddr,
		originalDst: GetOriginalDst,
		draining:    make(chan struct{}),
		tunnels:     make(map[*tunnel]struct{}),
	}
	s.httpServer = &http.Server{
		Handler:      handler,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}
	return s
}

// ListenAndServe listens on the server's address and serves it (see Serve).
func (s *Server) ListenAndServe() error {
	ln, err := net.Listen("tcp", s.listenAddr)
	if err != nil {
		return err
	}
	return s.Serve(ln)
}

// Serve multiplexes ln: HTTP/1.x requests get lane routing, everything else
// is tunnelled to its original destination. After Shutdown it returns
// ErrServerClosed.
func (s *Server) Serve(ln net.Listener) error {
	mux := cmux.New(timedListener{ln})
	// HTTP matcher: match requests starting with an HTTP method
	httpLn := mux.Match(httpMethodMatcher())
	// Everything else: TCP passthrough
	tcpLn := mux.Match(cmux.Any())

	s.mu.Lock()
	if s.closing {
		s.mu.Unlock()
		ln.Close()
		return ErrServerClosed
	}
	s.listener, s.mux = ln, mux
	s.mu.Unlock()
	log.Printf("[proxy] listening on %s", ln.Addr())

	go s.httpServer.Serve(httpLn)
	go s.serveTCPPassthrough(tcpLn)

	err := mux.Serve()
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closing {
		return ErrServerClosed
	}
	return err
}

// Serving reports whether the server has its listener bound and is taking
// connections: false before Serve and again once Shutdown has begun.
func (s *Server) Serving() bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.listener != nil && !s.closing
}

// Shutdown stops accepting connections and closes every tunnel the app has
// stopped sending on (and each one the app stops sending on from then on)
// without waiting for its upstream's answer. It waits for in-flight HTTP
// requests and the tunnels the app still holds open to finish on their own.
// If ctx ends first, it closes whatever is still open and returns ctx's
// error.
func (s *Server) Shutdown(ctx context.Context) error {
	s.mu.Lock()
	if !s.closing {
		s.closing = true
		close(s.draining)
	}
	ln, mux := s.listener, s.mux
	s.mu.Unlock()
	if ln != nil {
		// cmux.Close only stops handing out connections; closing the
		// listener is what stops accepting them.
		mux.Close()
		ln.Close()
	}

	httpErr := s.httpServer.Shutdown(ctx)
	if errors.Is(httpErr, net.ErrClosed) {
		// cmux backs the HTTP listener with ln, already closed above.
		httpErr = nil
	}
	if httpErr != nil {
		s.httpServer.Close()
	}
	if err := s.waitTunnels(ctx); err != nil {
		return err
	}
	return httpErr
}

// waitTunnels waits for every open tunnel to finish. If ctx ends first, it
// logs and closes each tunnel still open, and returns ctx's error.
func (s *Server) waitTunnels(ctx context.Context) error {
	done := make(chan struct{})
	go func() {
		s.open.Wait()
		close(done)
	}()
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		s.mu.Lock()
		now := time.Now()
		for t := range s.tunnels {
			t.logStillOpen(now)
			t.close()
		}
		s.mu.Unlock()
		return ctx.Err()
	}
}

// track registers t as an open tunnel; once Shutdown has begun it refuses.
func (s *Server) track(t *tunnel) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.closing {
		return false
	}
	s.tunnels[t] = struct{}{}
	s.open.Add(1)
	return true
}

func (s *Server) untrack(t *tunnel) {
	s.mu.Lock()
	delete(s.tunnels, t)
	s.mu.Unlock()
	s.open.Done()
}

// serveTCPPassthrough accepts non-HTTP connections and tunnels them to
// their original destination using SO_ORIGINAL_DST.
func (s *Server) serveTCPPassthrough(ln net.Listener) {
	for {
		conn, err := ln.Accept()
		if err != nil {
			return
		}
		go s.handleTCPConn(conn)
	}
}

func (s *Server) handleTCPConn(conn net.Conn) {
	defer conn.Close()

	// Unwrap to get the raw TCP connection for SO_ORIGINAL_DST
	rawConn := unwrapTCPConn(conn)
	if rawConn == nil {
		log.Printf("[proxy] tcp passthrough: cannot unwrap to TCPConn")
		return
	}

	origDst, err := s.originalDst(rawConn)
	if err != nil {
		log.Printf("[proxy] get original dst: %v", err)
		return
	}
	// A redirected connection always lands somewhere other than where it
	// was headed. One that was not redirected (anything dialing this port
	// directly) is headed for the proxy itself: dialing that would land
	// here again, and the new connection would do the same, without end.
	if sameTCPAddr(origDst, rawConn.LocalAddr()) {
		log.Printf("[proxy] refusing %s: addressed to the proxy itself", conn.RemoteAddr())
		return
	}

	t := newTunnel(conn, origDst)
	if !s.track(t) {
		return
	}
	defer s.untrack(t)

	upstream, err := net.DialTimeout("tcp", origDst.String(), 5*time.Second)
	if err != nil {
		log.Printf("[proxy] dial original dst %s: %v", origDst, err)
		return
	}

	t.run(upstream, s.draining)
}

// sameTCPAddr reports whether a and b are the same IP and port.
func sameTCPAddr(a, b net.Addr) bool {
	ta, ok := a.(*net.TCPAddr)
	tb, ok2 := b.(*net.TCPAddr)
	return ok && ok2 && ta.Port == tb.Port && ta.IP.Equal(tb.IP)
}

// unwrapTCPConn extracts the underlying *net.TCPConn from a possibly
// wrapped connection. cmux.MuxConn embeds net.Conn, so we access it
// via the embedded field.
func unwrapTCPConn(conn net.Conn) *net.TCPConn {
	switch c := conn.(type) {
	case *net.TCPConn:
		return c
	case *timedConn:
		return c.TCPConn
	case *cmux.MuxConn:
		// cmux.MuxConn embeds net.Conn
		return unwrapTCPConn(c.Conn)
	}
	return nil
}

// httpMethodMatcher returns a cmux matcher that matches HTTP/1.x requests
// by checking for a full HTTP method followed by a space.
func httpMethodMatcher() cmux.Matcher {
	methods := []string{
		"GET ", "PUT ", "POST ", "HEAD ",
		"DELETE ", "PATCH ", "OPTIONS ", "TRACE ",
		// CONNECT 不匹配——代理隧道请求走 TCP passthrough（SO_ORIGINAL_DST 转发到原始代理）
	}
	return func(r io.Reader) bool {
		buf := make([]byte, 8)
		n, err := io.ReadAtLeast(r, buf, 4)
		if err != nil {
			return false
		}
		data := buf[:n]
		for _, method := range methods {
			if len(data) >= len(method) {
				match := true
				for i := 0; i < len(method); i++ {
					if data[i] != method[i] {
						match = false
						break
					}
				}
				if match {
					return true
				}
			}
		}
		return false
	}
}
