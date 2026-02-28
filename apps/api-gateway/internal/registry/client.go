package registry

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync"
	"time"
)

// ServiceInfo mirrors lite-registry's response structure.
type ServiceInfo struct {
	Lanes []string `json:"lanes"`
	Port  int      `json:"port"`
}

// Client polls lite-registry and caches the service routing table.
type Client struct {
	registryURL string
	httpClient  *http.Client

	mu       sync.RWMutex
	services map[string]ServiceInfo
}

// NewClient creates a registry client that polls at the given interval.
func NewClient(registryURL string, pollInterval time.Duration) *Client {
	c := &Client{
		registryURL: registryURL,
		httpClient:  &http.Client{Timeout: 5 * time.Second},
		services:    make(map[string]ServiceInfo),
	}

	// Initial fetch (best effort)
	c.poll()

	go c.pollLoop(pollInterval)
	return c
}

func (c *Client) pollLoop(interval time.Duration) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for range ticker.C {
		c.poll()
	}
}

func (c *Client) poll() {
	url := c.registryURL + "/v1/routes"
	resp, err := c.httpClient.Get(url)
	if err != nil {
		slog.Warn("registry poll failed", "error", err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		slog.Warn("registry poll non-200", "status", resp.StatusCode)
		return
	}

	var payload struct {
		Services map[string]ServiceInfo `json:"services"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&payload); err != nil {
		slog.Warn("registry poll decode failed", "error", err)
		return
	}

	c.mu.Lock()
	c.services = payload.Services
	c.mu.Unlock()
	slog.Debug("registry poll success", "services", len(payload.Services))
}

// Resolve determines the target host and port for a service+lane combination.
// If the lane exists for the service, returns "{service}-{lane}:{port}".
// Otherwise falls back to "{service}:{port}" (prod).
func (c *Client) Resolve(service, lane string, defaultPort int) (host string, port int) {
	c.mu.RLock()
	info, ok := c.services[service]
	c.mu.RUnlock()

	port = defaultPort
	if ok && info.Port > 0 {
		port = info.Port
	}

	host = service
	if lane != "" && lane != "prod" {
		if ok && hasLane(info.Lanes, lane) {
			host = fmt.Sprintf("%s-%s", service, lane)
		}
	}
	return host, port
}

func hasLane(lanes []string, lane string) bool {
	for _, l := range lanes {
		if l == lane {
			return true
		}
	}
	return false
}
