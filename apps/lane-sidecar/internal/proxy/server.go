// Package proxy provides a transparent reverse proxy that intercepts outbound
// traffic (via iptables REDIRECT) and routes HTTP requests to lane-specific
// service instances based on the x-ctx-lane header. Non-HTTP traffic is
// passed through to the original destination via TCP tunneling.
package proxy

import (
	"bufio"
	"context"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"sync"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/registry"
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

// Server accepts TCP connections, detects the protocol, and routes:
//   - HTTP traffic → lane-aware reverse proxy
//   - Non-HTTP traffic → TCP passthrough to original destination
type Server struct {
	handler    *Handler
	listenAddr string
	listener   net.Listener
	httpServer *http.Server
	httpLn     *chanListener

	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

// NewServer creates a Server that listens on listenAddr (e.g. ":15001").
func NewServer(listenAddr string, resolver registry.Resolver) *Server {
	handler := NewHandler(resolver, nil)
	ctx, cancel := context.WithCancel(context.Background())
	httpLn := &chanListener{ch: make(chan net.Conn), done: make(chan struct{})}

	s := &Server{
		handler:    handler,
		listenAddr: listenAddr,
		httpLn:     httpLn,
		ctx:        ctx,
		cancel:     cancel,
	}
	s.httpServer = &http.Server{
		Handler:      handler,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 60 * time.Second,
	}
	return s
}

// ListenAndServe starts the proxy server.
func (s *Server) ListenAndServe() error {
	ln, err := net.Listen("tcp", s.listenAddr)
	if err != nil {
		return err
	}
	s.listener = ln
	log.Printf("[proxy] listening on %s", s.listenAddr)

	// Start HTTP server on the channel-based listener
	go s.httpServer.Serve(s.httpLn)

	for {
		conn, err := ln.Accept()
		if err != nil {
			select {
			case <-s.ctx.Done():
				return http.ErrServerClosed
			default:
				log.Printf("[proxy] accept error: %v", err)
				continue
			}
		}
		s.wg.Add(1)
		go func() {
			defer s.wg.Done()
			s.handleConn(conn)
		}()
	}
}

// Shutdown gracefully shuts down the server.
func (s *Server) Shutdown(ctx context.Context) error {
	s.cancel()
	if s.listener != nil {
		s.listener.Close()
	}
	close(s.httpLn.done)
	s.httpServer.Shutdown(ctx)
	s.wg.Wait()
	return nil
}

func (s *Server) handleConn(conn net.Conn) {
	defer conn.Close()

	// Peek at first bytes to detect protocol (need enough for "OPTIONS ")
	br := bufio.NewReader(conn)
	peeked, err := br.Peek(8)
	if err != nil {
		// Short read — try with what we have (minimum 4 bytes for "GET ")
		peeked, err = br.Peek(4)
		if err != nil {
			return
		}
	}

	peekConn := &bufferedConn{Conn: conn, reader: br, done: make(chan struct{})}

	if isHTTPRequest(peeked) {
		// HTTP → hand off to http.Server via channel listener
		s.httpLn.ch <- peekConn
		// Wait for http.Server to finish with this connection
		<-peekConn.done
	} else {
		// Non-HTTP → TCP passthrough to original destination
		s.tcpPassthrough(peekConn)
	}
}

// tcpPassthrough tunnels the connection to the original destination.
func (s *Server) tcpPassthrough(conn net.Conn) {
	tcpConn, ok := conn.(*bufferedConn)
	if !ok {
		log.Printf("[proxy] non-TCP connection, closing")
		return
	}

	origConn, ok := tcpConn.Conn.(*net.TCPConn)
	if !ok {
		log.Printf("[proxy] cannot get original dst: not a TCPConn")
		return
	}

	origDst, err := GetOriginalDst(origConn)
	if err != nil {
		log.Printf("[proxy] get original dst: %v", err)
		return
	}

	upstream, err := net.DialTimeout("tcp", origDst.String(), 5*time.Second)
	if err != nil {
		log.Printf("[proxy] dial original dst %s: %v", origDst, err)
		return
	}
	defer upstream.Close()

	// Bidirectional copy
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		io.Copy(upstream, conn)
	}()
	go func() {
		defer wg.Done()
		io.Copy(conn, upstream)
	}()
	wg.Wait()
}

// httpMethods lists all HTTP/1.x methods followed by a space.
// We check the full method + space to avoid false positives from binary
// protocols whose first bytes happen to match a single HTTP letter.
var httpMethods = []string{
	"GET ", "PUT ", "POST ", "HEAD ",
	"DELETE ", "PATCH ", "OPTIONS ", "CONNECT ", "TRACE ",
}

// isHTTPRequest checks if peeked bytes look like the start of an HTTP request.
func isHTTPRequest(peeked []byte) bool {
	for _, method := range httpMethods {
		if len(peeked) >= len(method) {
			match := true
			for i := 0; i < len(method); i++ {
				if peeked[i] != method[i] {
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

// bufferedConn wraps a net.Conn with a bufio.Reader for peeked data.
type bufferedConn struct {
	net.Conn
	reader   *bufio.Reader
	done     chan struct{}
	closeOnce sync.Once
}

func (c *bufferedConn) Read(b []byte) (int, error) {
	return c.reader.Read(b)
}

func (c *bufferedConn) Close() error {
	c.closeOnce.Do(func() { close(c.done) })
	return c.Conn.Close()
}

// chanListener feeds connections from a channel to http.Server.
type chanListener struct {
	ch   chan net.Conn
	done chan struct{}
}

func (l *chanListener) Accept() (net.Conn, error) {
	select {
	case conn := <-l.ch:
		return conn, nil
	case <-l.done:
		return nil, net.ErrClosed
	}
}

func (l *chanListener) Close() error {
	return nil
}

func (l *chanListener) Addr() net.Addr {
	return &net.TCPAddr{IP: net.IPv4zero, Port: 15001}
}
