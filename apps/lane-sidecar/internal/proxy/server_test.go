package proxy

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/chiwei-platform/lane-sidecar/internal/registry"
)

type mockResolver struct {
	services map[string]registry.ServiceInfo
}

func (m *mockResolver) Lookup(service string) (registry.ServiceInfo, bool) {
	info, ok := m.services[service]
	return info, ok
}

func (m *mockResolver) ResolveHost(host, lane string) string {
	if lane == "" || lane == "prod" {
		return host
	}
	svc, port := splitHostPort(host)
	info, ok := m.services[svc]
	if !ok || !info.HasLane(lane) {
		return host
	}
	if port != "" {
		return svc + "-" + lane + ":" + port
	}
	return svc + "-" + lane
}

func splitHostPort(host string) (string, string) {
	for i := len(host) - 1; i >= 0; i-- {
		if host[i] == ':' {
			return host[:i], host[i+1:]
		}
	}
	return host, ""
}

func TestHandler_RoutesToLaneInstance(t *testing.T) {
	laneBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("lane-response"))
	}))
	defer laneBackend.Close()

	reg := &mockResolver{
		services: map[string]registry.ServiceInfo{
			"agent-service": {Lanes: []string{"dev"}, Port: 8000},
		},
	}

	handler := NewHandler(reg, func(host string) string {
		return laneBackend.Listener.Addr().String()
	})

	req := httptest.NewRequest("GET", "http://agent-service:8000/api/chat", nil)
	req.Header.Set("x-ctx-lane", "dev")
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	body, _ := io.ReadAll(w.Result().Body)
	if string(body) != "lane-response" {
		t.Fatalf("expected 'lane-response', got %q", string(body))
	}
}

func TestHandler_FallbackWhenNoLane(t *testing.T) {
	prodBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("prod-response"))
	}))
	defer prodBackend.Close()

	reg := &mockResolver{
		services: map[string]registry.ServiceInfo{
			"agent-service": {Lanes: []string{"dev"}, Port: 8000},
		},
	}

	handler := NewHandler(reg, func(host string) string {
		return prodBackend.Listener.Addr().String()
	})

	req := httptest.NewRequest("GET", "http://agent-service:8000/api/chat", nil)
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	body, _ := io.ReadAll(w.Result().Body)
	if string(body) != "prod-response" {
		t.Fatalf("expected 'prod-response', got %q", string(body))
	}
}

func TestHttpMethodMatcher(t *testing.T) {
	matcher := httpMethodMatcher()

	httpStarts := []string{
		"GET /api/test HTTP/1.1",
		"POST /data HTTP/1.1",
		"PUT /foo HTTP/1.1",
		"DELETE /bar HTTP/1.1",
		"HEAD /health HTTP/1.1",
		"PATCH /patch HTTP/1.1",
		"OPTIONS /opt HTTP/1.1",
		"TRACE /trace HTTP/1.1",
	}
	for _, s := range httpStarts {
		r := strings.NewReader(s)
		if !matcher(r) {
			t.Errorf("expected %q to match HTTP", s)
		}
	}

	nonHTTPStarts := []string{
		"CONNECT host:443 HTTP/1.1",          // proxy tunnel → TCP passthrough
		"\x16\x03\x01\x00\x01\x00\x00\x00", // TLS ClientHello
		"\x00\x00\x00\x45\x00\x00\x00\x00", // MongoDB wire protocol
		"\x44\x00\x00\x00\x00\x00\x00\x00", // Looks like 'D' but not "DELETE "
		"AMQP\x00\x00\x09\x01",              // RabbitMQ AMQP
		"\x00\x00\x00\x34\x00\x00\x00\x00", // PostgreSQL startup
		"*3\r\n$3\r\n",                       // Redis RESP
	}
	for _, s := range nonHTTPStarts {
		r := strings.NewReader(s)
		if matcher(r) {
			t.Errorf("did not expect %q to match HTTP", s)
		}
	}
}

func TestHandler_ExternalTrafficPassthrough(t *testing.T) {
	externalBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Write([]byte("external-response"))
	}))
	defer externalBackend.Close()

	reg := &mockResolver{services: map[string]registry.ServiceInfo{}}

	handler := NewHandler(reg, func(host string) string {
		return externalBackend.Listener.Addr().String()
	})

	req := httptest.NewRequest("GET", "http://api.openai.com/v1/chat", nil)
	req.Header.Set("x-ctx-lane", "dev")
	w := httptest.NewRecorder()
	handler.ServeHTTP(w, req)

	body, _ := io.ReadAll(w.Result().Body)
	if string(body) != "external-response" {
		t.Fatalf("expected 'external-response', got %q", string(body))
	}
}
