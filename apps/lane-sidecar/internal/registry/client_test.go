package registry

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestClient_Lookup(t *testing.T) {
	routes := map[string]any{
		"services": map[string]any{
			"agent-service": map[string]any{
				"lanes": []string{"dev", "feat-test"},
				"port":  8000,
			},
			"lark-server": map[string]any{
				"lanes": []string{"dev"},
				"port":  3000,
			},
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(routes)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 100*time.Millisecond)
	defer c.Stop()
	time.Sleep(200 * time.Millisecond)

	info, ok := c.Lookup("agent-service")
	if !ok {
		t.Fatal("expected agent-service to be found")
	}
	if info.Port != 8000 {
		t.Fatalf("expected port 8000, got %d", info.Port)
	}
	if !info.HasLane("feat-test") {
		t.Fatal("expected feat-test lane")
	}
	if info.HasLane("staging") {
		t.Fatal("did not expect staging lane")
	}
	_, ok = c.Lookup("unknown-service")
	if ok {
		t.Fatal("did not expect unknown-service")
	}
}

func TestClient_ResolveHost(t *testing.T) {
	routes := map[string]any{
		"services": map[string]any{
			"agent-service": map[string]any{
				"lanes": []string{"dev"},
				"port":  8000,
			},
		},
	}
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		json.NewEncoder(w).Encode(routes)
	}))
	defer srv.Close()

	c := NewClient(srv.URL, 100*time.Millisecond)
	defer c.Stop()
	time.Sleep(200 * time.Millisecond)

	tests := []struct {
		host string
		lane string
		want string
	}{
		{"agent-service:8000", "dev", "agent-service-dev:8000"},
		{"agent-service:8000", "prod", "agent-service:8000"},
		{"agent-service:8000", "", "agent-service:8000"},
		{"agent-service:8000", "staging", "agent-service:8000"},
		{"external-api.com:443", "dev", "external-api.com:443"},
	}
	for _, tt := range tests {
		got := c.ResolveHost(tt.host, tt.lane)
		if got != tt.want {
			t.Errorf("ResolveHost(%q, %q) = %q, want %q", tt.host, tt.lane, got, tt.want)
		}
	}
}
