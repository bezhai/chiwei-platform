package proxy

import (
	"bytes"
	"errors"
	"io"
	"log"
	"net"
	"os"
	"regexp"
	"strings"
	"sync"
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
	return p.openAfter(t, 0)
}

// openAfter is open with the app waiting delay between connecting and
// sending its greeting.
func (p *passthrough) openAfter(t *testing.T, delay time.Duration) (app, up *net.TCPConn) {
	t.Helper()
	c, err := net.Dial("tcp", p.addr)
	if err != nil {
		t.Fatal(err)
	}
	app = c.(*net.TCPConn)
	t.Cleanup(func() { app.Close() })
	time.Sleep(delay)
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

// tunnelLine is how every log line about one tunnel through p starts.
func (p *passthrough) tunnelLine() string {
	return "[proxy] tunnel to " + p.upstream.Addr().String() + " "
}

// logCapture collects what the package logs while a test runs.
type logCapture struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func captureLog(t *testing.T) *logCapture {
	t.Helper()
	c := &logCapture{}
	prev := log.Writer()
	log.SetOutput(c)
	t.Cleanup(func() { log.SetOutput(prev) })
	return c
}

func (c *logCapture) Write(p []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.Write(p)
}

func (c *logCapture) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.String()
}

// lines returns the logged lines that contain every one of parts.
func (c *logCapture) lines(parts ...string) []string {
	var found []string
next:
	for _, line := range strings.Split(c.String(), "\n") {
		for _, part := range parts {
			if !strings.Contains(line, part) {
				continue next
			}
		}
		found = append(found, line)
	}
	return found
}

// waitLine waits for the one logged line that contains every one of parts.
func (c *logCapture) waitLine(t *testing.T, parts ...string) string {
	t.Helper()
	deadline := time.Now().Add(waitLimit)
	for {
		switch found := c.lines(parts...); {
		case len(found) == 1:
			return found[0]
		case len(found) > 1:
			t.Fatalf("%d log lines with %q, want one:\n%s", len(found), parts, strings.Join(found, "\n"))
		case time.Now().After(deadline):
			t.Fatalf("no log line with %q within %s; logged:\n%s", parts, waitLimit, c)
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// loggedDuration parses the duration that pattern's one group picks out of
// line.
func loggedDuration(t *testing.T, line, pattern string) time.Duration {
	t.Helper()
	m := regexp.MustCompile(pattern).FindStringSubmatch(line)
	if m == nil {
		t.Fatalf("no %q in log line %q", pattern, line)
	}
	d, err := time.ParseDuration(m[1])
	if err != nil {
		t.Fatalf("log line %q: %v", line, err)
	}
	return d
}

func expectContains(t *testing.T, line string, parts ...string) {
	t.Helper()
	for _, part := range parts {
		if !strings.Contains(line, part) {
			t.Errorf("log line %q lacks %q", line, part)
		}
	}
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
		newTunnel(appSide, upSide.RemoteAddr()).run(upSide, nil)
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

// A tunnel whose upstream ends before sending anything back gets one log
// line. Its bytes up tell an app message that never reached the upstream
// (next to none) from one the upstream got and never answered (all of it).
func TestPassthrough_LogsUpstreamThatSentNothingBack(t *testing.T) {
	ends := []struct {
		name string
		end  func(*testing.T, *net.TCPConn)
		how  string
	}{
		{"upstream closes", func(t *testing.T, c *net.TCPConn) { c.Close() }, " with EOF"},
		{"upstream resets", abort, " with error: "},
	}
	for _, e := range ends {
		t.Run(e.name, func(t *testing.T) {
			logs := captureLog(t)
			p := startPassthrough(t)
			app, up := p.openAfter(t, 100*time.Millisecond)
			app.Write([]byte("HELLO"))
			expectRead(t, up, "HELLO")
			time.Sleep(100 * time.Millisecond)

			e.end(t, up)
			expectClosed(t, app)
			app.Close()

			line := logs.waitLine(t, p.tunnelLine())
			t.Log(line)
			expectContains(t, line, "ended with nothing back from the upstream: ",
				"9 bytes up, 0 down", "upstream ended at ", e.how)
			if d := loggedDuration(t, line, `client's first byte at (\S+?),`); d < 100*time.Millisecond {
				t.Errorf("client's first byte logged at %s; the app waited 100ms after connecting", d)
			}
			if d := loggedDuration(t, line, `upstream ended at (\S+) with`); d < 200*time.Millisecond {
				t.Errorf("upstream end logged at %s; it came at least 200ms after the accept", d)
			}
		})
	}
}

// Tunnels that end the ordinary way log nothing: the proxy runs in every
// pod.
func TestPassthrough_LogsNothingForOrdinaryTunnels(t *testing.T) {
	cases := []struct {
		name   string
		finish func(t *testing.T, app, up *net.TCPConn)
	}{
		{"upstream answers, then closes", func(t *testing.T, app, up *net.TCPConn) {
			up.Write([]byte("PONG"))
			up.Close()
			expectRead(t, app, "PONG")
			expectEOF(t, app)
			app.Close()
		}},
		{"app closes, then the upstream does without answering", func(t *testing.T, app, up *net.TCPConn) {
			app.Close()
			expectEOF(t, up)
			up.Close()
		}},
		{"app closes, then the drain closes the tunnel", func(t *testing.T, app, up *net.TCPConn) {
			app.Close()
			expectEOF(t, up)
		}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			logs := captureLog(t)
			p := startPassthrough(t)
			app, up := p.open(t)

			c.finish(t, app, up)
			// Shutdown returns once every tunnel has finished, logging included.
			expectDrained(t, shutdownWithin(p.srv, time.Minute))

			if found := logs.lines(p.tunnelLine()); len(found) > 0 {
				t.Fatalf("logged an ordinary tunnel:\n%s", strings.Join(found, "\n"))
			}
		})
	}
}
