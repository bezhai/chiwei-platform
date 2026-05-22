package loader

import (
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"sync/atomic"
	"time"

	"github.com/chiwei-platform/api-gateway/internal/route"
)

// payload mirrors GET /internal/gateway-rules, which serializes
// paas-engine's domain.GatewaySnapshot: "version" is a bare JSON number
// (int64), not a quoted string.
type payload struct {
	Version   int64        `json:"version"`
	UpdatedAt string       `json:"updated_at"`
	Rules     []route.Rule `json:"rules"`
}

// Loader polls paas-engine for the routing snapshot and holds the current
// (last-good) snapshot behind an atomic pointer. current is nil until the first
// successful, validated fetch (cold start). It is only ever replaced by a
// validated snapshot, so on any failure it remains the last-good.
type Loader struct {
	url        string
	httpClient *http.Client
	current    atomic.Pointer[route.Snapshot]
}

// New creates a Loader. baseURL is the paas-engine base (e.g.
// http://paas-engine:8080); the /internal/gateway-rules path is appended.
func New(baseURL string) *Loader {
	return &Loader{
		url:        baseURL + "/internal/gateway-rules",
		httpClient: &http.Client{Timeout: 5 * time.Second},
	}
}

// Current returns the current snapshot, or nil at cold start.
func (l *Loader) Current() *route.Snapshot { return l.current.Load() }

// Start does an initial fetch (best effort) then polls every interval in a
// background goroutine.
func (l *Loader) Start(interval time.Duration) {
	if err := l.fetchOnce(); err != nil {
		slog.Warn("gateway-rules initial fetch failed, will retry", "error", err)
	}
	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			if err := l.fetchOnce(); err != nil {
				slog.Warn("gateway-rules poll failed, keeping last-good", "error", err)
			}
		}
	}()
}

// fetchOnce fetches, validates, and atomically swaps the snapshot. On any
// failure it returns an error and leaves current unchanged (last-good).
func (l *Loader) fetchOnce() error {
	resp, err := l.httpClient.Get(l.url)
	if err != nil {
		return fmt.Errorf("fetch gateway-rules: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("gateway-rules non-200: %d", resp.StatusCode)
	}

	var p payload
	if err := json.NewDecoder(resp.Body).Decode(&p); err != nil {
		return fmt.Errorf("decode gateway-rules: %w", err)
	}

	if err := validate(p.Rules); err != nil {
		return fmt.Errorf("validate gateway-rules: %w", err)
	}

	snap := route.NewSnapshot(p.Version, p.Rules)
	l.current.Store(snap)
	slog.Info("gateway-rules snapshot loaded", "version", p.Version, "rules", len(p.Rules))
	return nil
}

// validate is api-gateway's light defensive check (NOT paas-engine's full
// business validation): rules must be non-empty, key fields non-nil/non-empty,
// port in range. Enough to prevent panics and uphold the three-layer fallback.
func validate(rules []route.Rule) error {
	if len(rules) == 0 {
		return fmt.Errorf("empty rules (treated as failure)")
	}
	anyEnabled := false
	for i, r := range rules {
		if r.Enabled {
			anyEnabled = true
		}
		if r.Name == "" {
			return fmt.Errorf("rule[%d]: empty name", i)
		}
		if r.Match.PathPrefix == "" {
			return fmt.Errorf("rule[%d] %q: empty path_prefix", i, r.Name)
		}
		if len(r.Targets) == 0 {
			return fmt.Errorf("rule[%d] %q: no targets", i, r.Name)
		}
		for j, t := range r.Targets {
			if t.Service == "" {
				return fmt.Errorf("rule[%d] %q target[%d]: empty service", i, r.Name, j)
			}
			if t.Port < 1 || t.Port > 65535 {
				return fmt.Errorf("rule[%d] %q target[%d]: port %d out of range", i, r.Name, j, t.Port)
			}
		}
	}
	// All rules disabled is semantically an empty snapshot: the matcher skips
	// every disabled rule, so swapping this in would 404 all paths. Treat as
	// failure and keep last-good.
	if !anyEnabled {
		return fmt.Errorf("all rules disabled (treated as failure)")
	}
	return nil
}
