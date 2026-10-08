package proxy

import (
	"bytes"
	"errors"
	"io"
	"net"
	"os"
	"sync/atomic"
	"testing"
	"time"
)

// waitLimit bounds every wait for the far end of a tunnel to see something:
// plenty for loopback, short enough that a close that never arrives fails
// the test instead of hanging it.
const waitLimit = 2 * time.Second

// passthrough is a running Server whose TCP passthrough sends every
// connection to one local upstream, standing in for the iptables REDIRECT.
type passthrough struct {
	srv      *Server
	addr     string
	upstream *net.TCPListener
	served   <-chan error // what Serve returned
}

func startPassthrough(t *testing.T) *passthrough {
	t.Helper()
	upstream, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { upstream.Close() })

	srv := NewServer("", &mockResolver{})
	srv.originalDst = func(*net.TCPConn) (net.Addr, error) { return upstream.Addr(), nil }
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	served := make(chan error, 1)
	go func() { served <- srv.Serve(ln) }()

	return &passthrough{srv: srv, addr: ln.Addr().String(), upstream: upstream.(*net.TCPListener), served: served}
}

// open connects through the passthrough and returns both ends of the
// tunnel: the app's connection and the one the upstream accepted. The app
// speaks first with a non-HTTP greeting, which is what makes cmux hand the
// connection to the passthrough.
func (p *passthrough) open(t *testing.T) (app, up *net.TCPConn) {
	t.Helper()
	c, err := net.Dial("tcp", p.addr)
	if err != nil {
		t.Fatal(err)
	}
	app = c.(*net.TCPConn)
	t.Cleanup(func() { app.Close() })
	if _, err := app.Write([]byte("PING")); err != nil {
		t.Fatal(err)
	}

	p.upstream.SetDeadline(time.Now().Add(waitLimit))
	u, err := p.upstream.Accept()
	if err != nil {
		t.Fatalf("upstream never got the tunnelled connection: %v", err)
	}
	up = u.(*net.TCPConn)
	t.Cleanup(func() { up.Close() })
	expectRead(t, up, "PING")
	return app, up
}

// tcpPair returns the two ends of one loopback TCP connection.
func tcpPair(t *testing.T) (dialed, accepted *net.TCPConn) {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	d, err := net.Dial("tcp", ln.Addr().String())
	if err != nil {
		t.Fatal(err)
	}
	a, err := ln.Accept()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { d.Close(); a.Close() })
	return d.(*net.TCPConn), a.(*net.TCPConn)
}

// abort closes c with a TCP reset instead of an orderly FIN.
func abort(t *testing.T, c *net.TCPConn) {
	t.Helper()
	if err := c.SetLinger(0); err != nil {
		t.Fatal(err)
	}
	c.Close()
}

func expectRead(t *testing.T, c net.Conn, want string) {
	t.Helper()
	c.SetReadDeadline(time.Now().Add(waitLimit))
	got := make([]byte, len(want))
	if _, err := io.ReadFull(c, got); err != nil {
		t.Fatalf("reading %q: %v", want, err)
	}
	if string(got) != want {
		t.Fatalf("read %q, want %q", got, want)
	}
}

// expectAll reads c to EOF and requires exactly want.
func expectAll(t *testing.T, c net.Conn, want []byte) {
	t.Helper()
	c.SetReadDeadline(time.Now().Add(waitLimit))
	got, err := io.ReadAll(c)
	if err != nil {
		t.Fatalf("read %d of %d bytes, then: %v", len(got), len(want), err)
	}
	if !bytes.Equal(got, want) {
		t.Fatalf("read %d bytes, want the %d that were sent", len(got), len(want))
	}
}

func expectEOF(t *testing.T, c net.Conn) {
	t.Helper()
	c.SetReadDeadline(time.Now().Add(waitLimit))
	n, err := c.Read(make([]byte, 1))
	if errors.Is(err, os.ErrDeadlineExceeded) {
		t.Fatalf("no EOF within %s: the close never reached this end", waitLimit)
	}
	if n != 0 || err != io.EOF {
		t.Fatalf("read %d bytes, %v; want EOF", n, err)
	}
}

// expectClosed is expectEOF for an end whose peer went away abruptly, where
// a reset is as good an answer as EOF; only silence is wrong.
func expectClosed(t *testing.T, c net.Conn) {
	t.Helper()
	c.SetReadDeadline(time.Now().Add(waitLimit))
	n, err := c.Read(make([]byte, 1))
	if errors.Is(err, os.ErrDeadlineExceeded) {
		t.Fatalf("still open after %s: the other end's failure never reached this end", waitLimit)
	}
	if n != 0 || err == nil {
		t.Fatalf("read %d bytes, %v; want the connection closed", n, err)
	}
}

func TestPassthrough_UpstreamCloseReachesApp(t *testing.T) {
	p := startPassthrough(t)
	app, up := p.open(t)

	up.Write([]byte("bye"))
	up.Close()

	expectRead(t, app, "bye")
	expectEOF(t, app)
}

func TestPassthrough_AppCloseReachesUpstream(t *testing.T) {
	p := startPassthrough(t)
	app, up := p.open(t)

	app.Close()

	expectEOF(t, up)
}

func TestPassthrough_UpstreamResetClosesApp(t *testing.T) {
	p := startPassthrough(t)
	app, up := p.open(t)

	abort(t, up)

	expectClosed(t, app)
}

// A half-close ends one direction only: what the other side still has to
// send must arrive whole.
func TestPassthrough_HalfCloseKeepsOtherDirection(t *testing.T) {
	payload := bytes.Repeat([]byte("0123456789abcdef"), 64*1024)

	t.Run("app finishes sending first", func(t *testing.T) {
		p := startPassthrough(t)
		app, up := p.open(t)

		app.CloseWrite()
		expectEOF(t, up)

		go func() {
			defer up.Close()
			up.Write(payload)
		}()
		expectAll(t, app, payload)
	})

	t.Run("upstream finishes sending first", func(t *testing.T) {
		p := startPassthrough(t)
		app, up := p.open(t)

		up.CloseWrite()
		expectEOF(t, app)

		go func() {
			defer app.Close()
			app.Write(payload)
		}()
		expectAll(t, up, payload)
	})
}

// A failure in one direction must not leave the tunnel waiting for the
// other side to notice: both ends close and tunnel returns, even though the
// upstream here never sends or closes anything.
func TestTunnel_ResetReleasesBothEnds(t *testing.T) {
	app, appSide := tcpPair(t)
	upSide, up := tcpPair(t)
	done := make(chan struct{})
	go func() {
		tunnel(appSide, upSide)
		close(done)
	}()

	abort(t, app)

	expectClosed(t, up)
	select {
	case <-done:
	case <-time.After(waitLimit):
		t.Fatalf("tunnel still running %s after the app reset its end", waitLimit)
	}
}

// countingListener counts the connections it accepts.
type countingListener struct {
	net.Listener
	accepted atomic.Int64
}

func (l *countingListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err == nil {
		l.accepted.Add(1)
	}
	return c, err
}

// A connection that reached the proxy without the iptables redirect (anything
// dialing the pod IP and proxy port directly) has the proxy itself as its
// original destination. Tunnelling it would dial the proxy again, whose new
// connection would do the same, without end.
func TestPassthrough_RefusesConnectionsAddressedToItself(t *testing.T) {
	cases := []struct {
		name string
		send func(*net.TCPConn)
	}{
		{"client sends data", func(c *net.TCPConn) { c.Write([]byte("PING")) }},
		{"client sends nothing and closes", func(c *net.TCPConn) { c.CloseWrite() }},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			srv := NewServer("", &mockResolver{})
			// What SO_ORIGINAL_DST reports for a connection that was not
			// redirected: the address it was accepted on.
			srv.originalDst = func(c *net.TCPConn) (net.Addr, error) { return c.LocalAddr(), nil }
			raw, err := net.Listen("tcp", "127.0.0.1:0")
			if err != nil {
				t.Fatal(err)
			}
			ln := &countingListener{Listener: raw}
			t.Cleanup(func() { ln.Close() })
			go srv.Serve(ln)

			c, err := net.Dial("tcp", raw.Addr().String())
			if err != nil {
				t.Fatal(err)
			}
			client := c.(*net.TCPConn)
			defer client.Close()
			tc.send(client)

			client.SetReadDeadline(time.Now().Add(waitLimit))
			n, err := client.Read(make([]byte, 1))
			if accepted := ln.accepted.Load(); accepted != 1 {
				t.Fatalf("proxy accepted %d connections for one client: it dialed itself", accepted)
			}
			if n != 0 || err == nil || errors.Is(err, os.ErrDeadlineExceeded) {
				t.Fatalf("read %d bytes, %v; want the proxy to close the connection", n, err)
			}
		})
	}
}
