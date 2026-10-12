package proxy

import (
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"sync"
	"sync/atomic"
	"time"

	"github.com/soheilhy/cmux"
)

// side names one end of a tunnel.
type side int

const (
	clientSide   side = iota // the app's connection, accepted by the proxy
	upstreamSide             // the proxy's connection to the original destination
)

func (s side) other() side {
	if s == clientSide {
		return upstreamSide
	}
	return clientSide
}

// sideEnd is how and when one side of a tunnel ended; zero while it is open.
type sideEnd struct {
	at    time.Time
	err   error // io.EOF when the side finished sending, else what failed
	first bool  // the other side was still open at the time
}

// tunnel is one passthrough connection between the app (client) and its
// original destination (upstream), with what a drain and the anomaly logs
// need to know about it.
type tunnel struct {
	client  net.Conn
	dst     net.Addr
	arrival *arrival
	sent    [2]atomic.Int64 // bytes written to each side

	mu       sync.Mutex
	upstream net.Conn // nil until dialed; run sets it before the relays start
	ended    [2]sideEnd
}

func newTunnel(client net.Conn, dst net.Addr) *tunnel {
	t := &tunnel{client: client, dst: dst, arrival: arrivalOf(client)}
	if t.arrival == nil {
		// Not accepted through Serve: time it from here.
		t.arrival = &arrival{accepted: time.Now()}
	}
	return t
}

// run relays bytes between the app and upstream until both directions are
// done, then closes both. A direction that reaches EOF half-closes the end it
// was writing to, so that peer sees the close while the other direction keeps
// flowing (clients that close a connection, or cancel a query, wait for the
// server's close and send nothing more). A direction that fails closes both
// ends, which also ends the other direction.
//
// Once draining is closed, a tunnel whose client has stopped sending is
// closed instead of waiting for the upstream's answer. The client is the
// app: as a native sidecar the proxy drains only after the app has exited,
// and an upstream that never answers the app's close (an HTTPS gateway
// still working on a request the app gave up on) would otherwise hold the
// drain until its cap. An app that is still running keeps its tunnels for
// as long as it holds them open. The cost: an app that half-closed and is
// still waiting for the answer when the drain begins loses that answer; it
// is shutting down by then.
func (t *tunnel) run(upstream net.Conn, draining <-chan struct{}) {
	t.mu.Lock()
	t.upstream = upstream
	t.mu.Unlock()

	toClientDone := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(2)
	go func() {
		defer wg.Done()
		t.relay(upstreamSide, clientSide)
		select {
		case <-toClientDone:
		case <-draining:
			t.close()
		}
	}()
	go func() {
		defer wg.Done()
		defer close(toClientDone)
		t.relay(clientSide, upstreamSide)
	}()
	wg.Wait()
	t.close()
	t.logIfUnanswered()
}

// relay copies what side from sends to side to until from ends.
func (t *tunnel) relay(to, from side) {
	_, err := io.Copy(endpoint{t, to}, endpoint{t, from})
	if err == nil {
		err = closeWrite(t.conn(to))
	}
	if err != nil {
		t.close()
	}
}

func (t *tunnel) conn(s side) net.Conn {
	if s == clientSide {
		return t.client
	}
	return t.upstream
}

// close closes both sides, which ends both relays.
func (t *tunnel) close() {
	t.client.Close()
	t.mu.Lock()
	up := t.upstream
	t.mu.Unlock()
	if up != nil {
		up.Close()
	}
}

// end records that side s ended with err. Only a side's first end counts,
// and a close the tunnel made itself (net.ErrClosed) is not the peer's.
func (t *tunnel) end(s side, err error) {
	if errors.Is(err, net.ErrClosed) {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if !t.ended[s].at.IsZero() {
		return
	}
	t.ended[s] = sideEnd{at: time.Now(), err: err, first: t.ended[s.other()].at.IsZero()}
}

// logIfUnanswered logs a finished tunnel whose upstream ended, by close or
// by error, before the app did and without sending anything back. Its bytes
// up tell an app message that never reached the upstream (next to none,
// say a TLS ClientHello stuck on the way) from one the upstream got and
// never answered (all of it).
func (t *tunnel) logIfUnanswered() {
	t.mu.Lock()
	up := t.ended[upstreamSide]
	t.mu.Unlock()
	if up.at.IsZero() || !up.first || t.sent[clientSide].Load() > 0 {
		return
	}
	log.Printf("[proxy] tunnel to %s ended with nothing back from the upstream: %s, upstream %s",
		t.dst, t.summary(time.Now()), t.describe(up))
}

// logStillOpen logs a tunnel that the drain cap is about to close.
func (t *tunnel) logStillOpen(now time.Time) {
	t.mu.Lock()
	ended := t.ended
	t.mu.Unlock()
	log.Printf("[proxy] tunnel to %s still open at the drain cap: %s, client %s, upstream %s",
		t.dst, t.summary(now), t.describe(ended[clientSide]), t.describe(ended[upstreamSide]))
}

// summary is what both log lines say about a tunnel: how long it has been
// open, the bytes written up (app to upstream, including the ones cmux read
// to pick the route) and down, and when the app's first byte came in.
func (t *tunnel) summary(now time.Time) string {
	first := "client sent nothing"
	if at := t.arrival.firstByte.Load(); at != nil {
		first = "client's first byte at " + t.since(*at)
	}
	return fmt.Sprintf("open for %s, %d bytes up, %d down, %s",
		t.since(now), t.sent[upstreamSide].Load(), t.sent[clientSide].Load(), first)
}

func (t *tunnel) describe(e sideEnd) string {
	switch {
	case e.at.IsZero():
		return "open"
	case e.err == io.EOF:
		return "ended at " + t.since(e.at) + " with EOF"
	default:
		return fmt.Sprintf("ended at %s with error: %v", t.since(e.at), e.err)
	}
}

// since is how long after the accept at came, to the millisecond.
func (t *tunnel) since(at time.Time) string {
	return at.Sub(t.arrival.accepted).Round(time.Millisecond).String()
}

// endpoint is one side of a tunnel as its relays use it: it counts the
// bytes written to it, and a read or a write that fails marks that side
// ended.
type endpoint struct {
	t    *tunnel
	side side
}

func (e endpoint) Read(p []byte) (int, error) {
	n, err := e.t.conn(e.side).Read(p)
	if err != nil {
		e.t.end(e.side, err)
	}
	return n, err
}

func (e endpoint) Write(p []byte) (int, error) {
	n, err := e.t.conn(e.side).Write(p)
	e.t.sent[e.side].Add(int64(n))
	if err != nil {
		e.t.end(e.side, err)
	}
	return n, err
}

// closeWrite sends FIN on c's socket and leaves its read side open.
func closeWrite(c net.Conn) error {
	tc := unwrapTCPConn(c)
	if tc == nil {
		return fmt.Errorf("cannot half-close %T", c)
	}
	return tc.CloseWrite()
}

// arrival is when the proxy accepted a connection and when the
// connection's first byte came in.
type arrival struct {
	accepted  time.Time
	firstByte atomic.Pointer[time.Time] // nil until a read returns data
}

// timedConn is an accepted connection that keeps its arrival. cmux reads
// the first bytes through it to pick a route, so they are timed whichever
// route the connection takes.
type timedConn struct {
	*net.TCPConn
	arrival
}

func (c *timedConn) Read(p []byte) (int, error) {
	n, err := c.TCPConn.Read(p)
	if n > 0 && c.firstByte.Load() == nil {
		now := time.Now()
		c.firstByte.CompareAndSwap(nil, &now)
	}
	return n, err
}

// timedListener stamps each connection it accepts with its arrival.
type timedListener struct{ net.Listener }

func (l timedListener) Accept() (net.Conn, error) {
	c, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	if tc, ok := c.(*net.TCPConn); ok {
		return &timedConn{TCPConn: tc, arrival: arrival{accepted: time.Now()}}, nil
	}
	return c, nil
}

// arrivalOf returns the arrival of a connection accepted through a
// timedListener, or nil.
func arrivalOf(conn net.Conn) *arrival {
	if mc, ok := conn.(*cmux.MuxConn); ok {
		conn = mc.Conn
	}
	if tc, ok := conn.(*timedConn); ok {
		return &tc.arrival
	}
	return nil
}
