package proxy

import (
	"io"
	"net/http"
	"net/http/httptest"
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
