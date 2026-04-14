// Package registry provides a polling client for the lite-registry service,
// which tracks which Kubernetes services have lane-specific instances.
package registry

import (
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Resolver abstracts service lookup and host resolution for lane routing.
// The proxy package uses this interface so it can be mocked in tests.
type Resolver interface {
	Lookup(service string) (ServiceInfo, bool)
	ResolveHost(host, lane string) string
}

// ServiceInfo describes a service's available lanes and port.
type ServiceInfo struct {
	Lanes []string `json:"lanes"`
	Port  int      `json:"port"`
}

// HasLane reports whether the service has a deployment in the given lane.
func (s ServiceInfo) HasLane(lane string) bool {
	for _, l := range s.Lanes {
		if l == lane {
			return true
		}
	}
	return false
}

// routesResponse is the JSON shape returned by lite-registry /v1/routes.
type routesResponse struct {
	Services  map[string]ServiceInfo `json:"services"`
	UpdatedAt string                 `json:"updated_at"`
}

// Client polls lite-registry and caches the latest route table in memory.
// It implements the Resolver interface.
type Client struct {
	registryURL  string
	pollInterval time.Duration
	httpClient   *http.Client

	mu       sync.RWMutex
	services map[string]ServiceInfo

	stopCh chan struct{}
	done   chan struct{}
}

// compile-time check that *Client satisfies Resolver.
var _ Resolver = (*Client)(nil)

// NewClient creates a Client that polls registryURL every pollInterval.
// The first poll is attempted immediately in the background.
func NewClient(registryURL string, pollInterval time.Duration) *Client {
	c := &Client{
		registryURL:  registryURL,
		pollInterval: pollInterval,
		httpClient:   &http.Client{Timeout: 5 * time.Second},
		services:     make(map[string]ServiceInfo),
		stopCh:       make(chan struct{}),
		done:         make(chan struct{}),
	}
	go c.pollLoop()
	return c
}

// Stop terminates the background polling goroutine and waits for it to exit.
func (c *Client) Stop() {
	close(c.stopCh)
	<-c.done
}

// Lookup returns the ServiceInfo for the given service name.
// The second return value is false if the service is not in the route table.
func (c *Client) Lookup(service string) (ServiceInfo, bool) {
	c.mu.RLock()
	defer c.mu.RUnlock()
	info, ok := c.services[service]
	return info, ok
}

// ResolveHost rewrites a "host:port" string to target a lane-specific service.
//
// Examples:
//
//	ResolveHost("agent-service:8000", "dev")     → "agent-service-dev:8000"
//	ResolveHost("agent-service:8000", "prod")    → "agent-service:8000"
//	ResolveHost("agent-service:8000", "")        → "agent-service:8000"
//	ResolveHost("agent-service:8000", "staging") → "agent-service:8000"  (lane not available)
//	ResolveHost("external-api.com:443", "dev")   → "external-api.com:443" (not in registry)
func (c *Client) ResolveHost(host, lane string) string {
	if lane == "" || lane == "prod" {
		return host
	}

	svcName, port, ok := splitHostPort(host)
	if !ok {
		return host
	}

	info, found := c.Lookup(svcName)
	if !found || !info.HasLane(lane) {
		return host
	}

	return fmt.Sprintf("%s-%s:%s", svcName, lane, port)
}

// pollLoop runs until Stop is called, fetching routes on each tick.
func (c *Client) pollLoop() {
	defer close(c.done)

	// Immediate first poll.
	c.fetchRoutes()

	ticker := time.NewTicker(c.pollInterval)
	defer ticker.Stop()

	for {
		select {
		case <-c.stopCh:
			return
		case <-ticker.C:
			c.fetchRoutes()
		}
	}
}

// fetchRoutes calls lite-registry and updates the cached service map.
func (c *Client) fetchRoutes() {
	url := strings.TrimRight(c.registryURL, "/") + "/v1/routes"
	resp, err := c.httpClient.Get(url)
	if err != nil {
		log.Printf("registry: poll failed: %v", err)
		return
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		log.Printf("registry: poll returned status %d", resp.StatusCode)
		return
	}

	var routes routesResponse
	if err := json.NewDecoder(resp.Body).Decode(&routes); err != nil {
		log.Printf("registry: decode failed: %v", err)
		return
	}

	c.mu.Lock()
	c.services = routes.Services
	c.mu.Unlock()
}

// splitHostPort splits "host:port" into its components.
// Returns ("", "", false) if there is no colon.
func splitHostPort(hostport string) (host, port string, ok bool) {
	idx := strings.LastIndex(hostport, ":")
	if idx < 0 {
		return "", "", false
	}
	return hostport[:idx], hostport[idx+1:], true
}
