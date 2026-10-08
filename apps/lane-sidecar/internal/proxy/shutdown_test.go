package proxy

import (
	"context"
	"errors"
	"net"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
	"time"
)

// shutdownWithin runs srv.Shutdown under a cap of limit and delivers its
// result on the returned channel.
func shutdownWithin(srv *Server, limit time.Duration) <-chan error {
	done := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), limit)
		defer cancel()
		done <- srv.Shutdown(ctx)
	}()
	return done
}

// expectRefused waits for addr to stop accepting connections.
func expectRefused(t *testing.T, addr string) {
	t.Helper()
	deadline := time.Now().Add(waitLimit)
	for time.Now().Before(deadline) {
		c, err := net.Dial("tcp", addr)
		if err != nil {
			return
		}
		c.Close()
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("%s still accepting connections %s into shutdown", addr, waitLimit)
}

// hungBackend is an HTTP server that takes requests and never answers
// them; arrived is closed once a request has reached it.
func hungBackend(t *testing.T) (addr string, arrived <-chan struct{}) {
	t.Helper()
	ch := make(chan struct{})
	var once sync.Once
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		once.Do(func() { close(ch) })
		<-r.Context().Done()
	}))
	t.Cleanup(func() {
		// Close waits for the handler, which waits for its connection.
		b.CloseClientConnections()
		b.Close()
	})
	return b.Listener.Addr().String(), ch
}

func TestShutdown_LetsOpenTunnelsFinish(t *testing.T) {
	p := startPassthrough(t)
	app, up := p.open(t)

	done := shutdownWithin(p.srv, time.Minute)
	expectRefused(t, p.addr)

	app.Write([]byte("more"))
	expectRead(t, up, "more")
	up.Write([]byte("reply"))
	expectRead(t, app, "reply")
	select {
	case err := <-done:
		t.Fatalf("Shutdown returned (%v) while a tunnel was still open", err)
	default:
	}

	app.Close()
	up.Close()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Shutdown: %v; want nil once every tunnel finished on its own", err)
		}
	case <-time.After(waitLimit):
		t.Fatalf("Shutdown still waiting %s after the last tunnel closed", waitLimit)
	}
	select {
	case err := <-p.served:
		if !errors.Is(err, ErrServerClosed) {
			t.Fatalf("Serve returned %v, want ErrServerClosed", err)
		}
	case <-time.After(waitLimit):
		t.Fatal("Serve still running after Shutdown")
	}
}

func TestShutdown_ClosesWhatOutlastsTheCap(t *testing.T) {
	p := startPassthrough(t)
	app, up := p.open(t)
	backend, arrived := hungBackend(t)
	req, err := net.Dial("tcp", p.addr)
	if err != nil {
		t.Fatal(err)
	}
	defer req.Close()
	req.Write([]byte("GET / HTTP/1.1\r\nHost: " + backend + "\r\n\r\n"))
	select {
	case <-arrived:
	case <-time.After(waitLimit):
		t.Fatal("request never reached the backend")
	}

	select {
	case err := <-shutdownWithin(p.srv, 200*time.Millisecond):
		if !errors.Is(err, context.DeadlineExceeded) {
			t.Fatalf("Shutdown: %v; want it to report that the cap ran out", err)
		}
	case <-time.After(waitLimit):
		t.Fatalf("Shutdown still blocked %s past a 200ms cap", waitLimit)
	}

	expectClosed(t, app)
	expectClosed(t, up)
	expectClosed(t, req)
}
