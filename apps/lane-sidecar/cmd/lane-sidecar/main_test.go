package main

import (
	"bytes"
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/chiwei-platform/lane-sidecar/internal/proxy"
)

// argsEnv carries main's flags to a child process. When it is set, this
// test binary runs main() instead of the tests, so the tests can signal a
// real lane-sidecar process and read its exit code.
const argsEnv = "LANE_SIDECAR_TEST_ARGS"

func TestMain(m *testing.M) {
	if args, ok := os.LookupEnv(argsEnv); ok {
		os.Args = append([]string{"lane-sidecar"}, strings.Fields(args)...)
		main()
		os.Exit(0)
	}
	os.Exit(m.Run())
}

// waitLimit bounds every wait on the child: plenty for loopback, short
// enough that a child that never exits fails the test instead of hanging it.
const waitLimit = 3 * time.Second

type sidecar struct {
	proxy  string
	health string // URL of the health endpoint
	cmd    *exec.Cmd
	exited chan struct{}
	err    error        // cmd.Wait's result; read only after exited is closed
	log    bytes.Buffer // the child's output; read only after exited is closed
}

// startSidecar runs main() in a child process on free loopback ports, with
// an empty lane registry, and returns once its proxy answers.
func startSidecar(t *testing.T, flags ...string) *sidecar {
	t.Helper()
	registry := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte(`{"services":{}}`))
	}))
	t.Cleanup(registry.Close)

	proxyPort, healthPort := freePort(t), freePort(t)
	args := append([]string{
		"-port=" + proxyPort,
		"-health-port=" + healthPort,
		"-registry-url=" + registry.URL,
	}, flags...)
	s := &sidecar{
		proxy:  "127.0.0.1:" + proxyPort,
		health: "http://127.0.0.1:" + healthPort + "/healthz",
		exited: make(chan struct{}),
	}
	s.cmd = exec.Command(os.Args[0])
	s.cmd.Env = append(os.Environ(), argsEnv+"="+strings.Join(args, " "))
	s.cmd.Stdout = &s.log
	s.cmd.Stderr = &s.log
	if err := s.cmd.Start(); err != nil {
		t.Fatal(err)
	}
	go func() {
		s.err = s.cmd.Wait()
		close(s.exited)
	}()
	t.Cleanup(func() {
		s.cmd.Process.Kill()
		<-s.exited
	})

	// Wait the way the kubelet's startup probe does.
	deadline := time.Now().Add(waitLimit)
	for {
		status, err := s.healthStatus()
		if status == http.StatusOK {
			return s
		}
		if time.Now().After(deadline) {
			s.cmd.Process.Kill()
			<-s.exited
			t.Fatalf("health not ok after %s: %d %v\n%s", waitLimit, status, err, &s.log)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func (s *sidecar) healthStatus() (int, error) {
	resp, err := http.Get(s.health)
	if err != nil {
		return 0, err
	}
	resp.Body.Close()
	return resp.StatusCode, nil
}

func freePort(t *testing.T) string {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	return strconv.Itoa(ln.Addr().(*net.TCPAddr).Port)
}

// proxiedClient sends every request through the sidecar's proxy port, the
// way the iptables redirect does for an app in the pod.
func proxiedClient(proxy string) *http.Client {
	return &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "tcp", proxy)
		},
	}}
}

func (s *sidecar) terminate(t *testing.T) {
	t.Helper()
	if err := s.cmd.Process.Signal(syscall.SIGTERM); err != nil {
		t.Fatal(err)
	}
}

func (s *sidecar) expectExitZero(t *testing.T) {
	t.Helper()
	select {
	case <-s.exited:
	case <-time.After(waitLimit):
		t.Fatalf("still running %s after SIGTERM", waitLimit)
	}
	if s.err != nil {
		t.Fatalf("exited with %v, want exit code 0\n%s", s.err, &s.log)
	}
}

// slowBackend is an HTTP server whose handler blocks until release is
// closed (or the request goes away); arrived is closed once a request has
// reached it.
func slowBackend(t *testing.T, body string) (url string, arrived, release chan struct{}) {
	t.Helper()
	arrived, release = make(chan struct{}), make(chan struct{})
	var once sync.Once
	b := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		once.Do(func() { close(arrived) })
		select {
		case <-release:
			w.Write([]byte(body))
		case <-r.Context().Done():
		}
	}))
	t.Cleanup(func() {
		// Close waits for the handler, which waits for its connection.
		b.CloseClientConnections()
		b.Close()
	})
	return b.URL, arrived, release
}

// sendThroughProxy issues a GET through the sidecar and delivers the body,
// or the error, on the returned channel.
func sendThroughProxy(proxy, url string) <-chan string {
	got := make(chan string, 1)
	go func() {
		resp, err := proxiedClient(proxy).Get(url)
		if err != nil {
			got <- "error: " + err.Error()
			return
		}
		defer resp.Body.Close()
		b, err := io.ReadAll(resp.Body)
		if err != nil {
			got <- "error: " + err.Error()
			return
		}
		got <- string(b)
	}()
	return got
}

func waitFor(t *testing.T, ch <-chan struct{}, what string) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(waitLimit):
		t.Fatalf("%s did not happen within %s", what, waitLimit)
	}
}

func TestSIGTERM_ExitsZero(t *testing.T) {
	s := startSidecar(t)

	s.terminate(t)

	s.expectExitZero(t)
}

func TestSIGTERM_LetsInFlightRequestFinish(t *testing.T) {
	s := startSidecar(t)
	url, arrived, release := slowBackend(t, "finished")
	got := sendThroughProxy(s.proxy, url)
	waitFor(t, arrived, "request reaching the backend")

	s.terminate(t)
	select {
	case <-s.exited:
		t.Fatalf("exited (%v) with a request still in flight\n%s", s.err, &s.log)
	case <-time.After(300 * time.Millisecond):
	}
	if status, err := s.healthStatus(); status != http.StatusServiceUnavailable {
		t.Errorf("health while draining: %d %v; want 503", status, err)
	}
	close(release)

	select {
	case body := <-got:
		if body != "finished" {
			t.Fatalf("in-flight request got %q, want the backend's answer", body)
		}
	case <-time.After(waitLimit):
		t.Fatal("in-flight request never got an answer")
	}
	s.expectExitZero(t)
}

func TestSIGTERM_DrainTimeoutBoundsExit(t *testing.T) {
	s := startSidecar(t, "-drain-timeout=300ms")
	url, arrived, _ := slowBackend(t, "never sent")
	sendThroughProxy(s.proxy, url)
	waitFor(t, arrived, "request reaching the backend")

	s.terminate(t)

	s.expectExitZero(t)
}

func expectHealth(t *testing.T, h http.Handler, want int, when string) {
	t.Helper()
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("GET", "/healthz", nil))
	if w.Code != want {
		t.Fatalf("health %s: %d, want %d", when, w.Code, want)
	}
}

// The app container starts once the sidecar's startup probe passes, so the
// probe must not pass before the proxy it is redirected to is listening.
func TestHealth_OKOnlyWhileProxyServes(t *testing.T) {
	srv := proxy.NewServer("", nil)
	h := health(srv)
	expectHealth(t, h, http.StatusServiceUnavailable, "before the proxy listens")

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	defer ln.Close()
	go srv.Serve(ln)
	deadline := time.Now().Add(waitLimit)
	for w := httptest.NewRecorder(); ; w = httptest.NewRecorder() {
		h.ServeHTTP(w, httptest.NewRequest("GET", "/healthz", nil))
		if w.Code == http.StatusOK {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("health still %d %s after Serve started", w.Code, waitLimit)
		}
		time.Sleep(10 * time.Millisecond)
	}

	if err := srv.Shutdown(context.Background()); err != nil {
		t.Fatal(err)
	}
	expectHealth(t, h, http.StatusServiceUnavailable, "once shutdown has begun")
}
