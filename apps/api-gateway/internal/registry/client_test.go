package registry

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"
)

func TestResolveWithLane(t *testing.T) {
	// Mock lite-registry server
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		resp := struct {
			Services map[string]ServiceInfo `json:"services"`
		}{
			Services: map[string]ServiceInfo{
				"lark-proxy": {Lanes: []string{"dev", "prod"}, Port: 3003},
				"paas-engine": {Lanes: []string{"prod"}, Port: 8080},
			},
		}
		json.NewEncoder(w).Encode(resp)
	}))
	defer srv.Close()

	client := NewClient(srv.URL, 1*time.Hour) // long interval, we only need initial poll

	// Wait a moment for initial poll
	time.Sleep(50 * time.Millisecond)

	tests := []struct {
		service     string
		lane        string
		defaultPort int
		wantHost    string
		wantPort    int
	}{
		{"lark-proxy", "dev", 3003, "lark-proxy-dev", 3003},
		{"lark-proxy", "prod", 3003, "lark-proxy", 3003},
		{"lark-proxy", "", 3003, "lark-proxy", 3003},
		{"paas-engine", "dev", 8080, "paas-engine", 8080}, // dev lane not in registry
		{"unknown-svc", "dev", 9090, "unknown-svc", 9090}, // unknown service
	}

	for _, tt := range tests {
		host, port := client.Resolve(tt.service, tt.lane, tt.defaultPort)
		if host != tt.wantHost || port != tt.wantPort {
			t.Errorf("Resolve(%q, %q, %d): got (%q, %d), want (%q, %d)",
				tt.service, tt.lane, tt.defaultPort, host, port, tt.wantHost, tt.wantPort)
		}
	}
}

func TestResolveRegistryDown(t *testing.T) {
	// Point to a non-existent server
	client := NewClient("http://127.0.0.1:1", 1*time.Hour)

	// Should fallback to default
	host, port := client.Resolve("paas-engine", "dev", 8080)
	if host != "paas-engine" || port != 8080 {
		t.Errorf("expected fallback, got (%q, %d)", host, port)
	}
}
