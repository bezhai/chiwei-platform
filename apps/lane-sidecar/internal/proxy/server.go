// Package proxy provides a transparent reverse proxy that intercepts outbound
// traffic (via iptables REDIRECT) and routes HTTP requests to lane-specific
// service instances based on the x-ctx-lane header. Non-HTTP traffic is
// passed through to the original destination via TCP tunneling.
package proxy

import (
	"context"
	"fmt"
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

// Server uses cmux to multiplex a single TCP listener into HTTP and non-HTTP
// streams. HTTP traffic gets lane-aware routing; everything else gets TCP
// passthrough to the original destination (via SO_ORIGINAL_DST).
type Server struct {
	handler    *Handler
	listenAddr string
	httpServer *http.Server
	mux        cmux.CMux
	listener   net.Listener
	// originalDst finds where a passthrough connection was headed. It is
	// GetOriginalDst in production; tests point it at a local upstream,
	// since SO_ORIGINAL_DST only answers behind an iptables REDIRECT.
	originalDst func(*net.TCPConn) (net.Addr, error)
}

// NewServer creates a Server that listens on listenAddr (e.g. ":15001").
func NewServer(listenAddr string, resolver registry.Resolver) *Server {
	handler := NewHandler(resolver, nil)
	s := &Server{
		handler:     handler,
		listenAddr:  listenAddr,
		originalDst: GetOriginalDst,
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
// is tunnelled to its original destination.
func (s *Server) Serve(ln net.Listener) error {
	s.listener = ln
	log.Printf("[proxy] listening on %s", ln.Addr())

	s.mux = cmux.New(ln)

	// HTTP matcher: match requests starting with an HTTP method
	httpLn := s.mux.Match(httpMethodMatcher())
	// Everything else: TCP passthrough
	tcpLn := s.mux.Match(cmux.Any())

	go s.httpServer.Serve(httpLn)
	go s.serveTCPPassthrough(tcpLn)

	return s.mux.Serve()
}

// Shutdown gracefully shuts down the server.
func (s *Server) Shutdown(ctx context.Context) error {
	if s.mux != nil {
		s.mux.Close()
	}
	return s.httpServer.Shutdown(ctx)
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

	upstream, err := net.DialTimeout("tcp", origDst.String(), 5*time.Second)
	if err != nil {
		log.Printf("[proxy] dial original dst %s: %v", origDst, err)
		return
	}

	tunnel(conn, upstream)
}

// tunnel relays bytes between client and upstream until both directions are
// done, then closes both. A direction that reaches EOF half-closes the end it
// was writing to, so that peer sees the close while the other direction keeps
// flowing (clients that close a connection, or cancel a query, wait for the
// server's close and send nothing more). A direction that fails closes both
// ends, which also ends the other direction.
func tunnel(client, upstream net.Conn) {
	closeBoth := func() {
		client.Close()
		upstream.Close()
	}
	relay := func(dst, src net.Conn) {
		_, err := io.Copy(dst, src)
		if err == nil {
			err = closeWrite(dst)
		}
		if err != nil {
			closeBoth()
		}
	}

	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		relay(upstream, client)
	}()
	go func() {
		defer wg.Done()
		relay(client, upstream)
	}()
	wg.Wait()
	closeBoth()
}

// closeWrite sends FIN on c's socket and leaves its read side open.
func closeWrite(c net.Conn) error {
	tc := unwrapTCPConn(c)
	if tc == nil {
		return fmt.Errorf("cannot half-close %T", c)
	}
	return tc.CloseWrite()
}

// unwrapTCPConn extracts the underlying *net.TCPConn from a possibly
// wrapped connection. cmux.MuxConn embeds net.Conn, so we access it
// via the embedded field.
func unwrapTCPConn(conn net.Conn) *net.TCPConn {
	if tc, ok := conn.(*net.TCPConn); ok {
		return tc
	}
	// cmux.MuxConn embeds net.Conn
	if mc, ok := conn.(*cmux.MuxConn); ok {
		return unwrapTCPConn(mc.Conn)
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
