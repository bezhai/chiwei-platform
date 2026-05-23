package route

import "strings"

// Matcher selects a rule for a request from a Snapshot's pre-sorted rules.
type Matcher struct {
	snapshot *Snapshot
}

// NewMatcher creates a Matcher over the given snapshot.
func NewMatcher(snapshot *Snapshot) *Matcher {
	return &Matcher{snapshot: snapshot}
}

// MatchResult is the outcome of a successful match.
type MatchResult struct {
	Rule Rule
	// Redirect is true when path matched a "/foo/" prefix without its trailing
	// slash (e.g. "/dashboard" for prefix "/dashboard/"); the gateway issues a
	// 301 to the slashed path. Preserves the legacy routes.yaml behavior.
	Redirect bool
}

// Match returns the highest-priority enabled rule whose path_prefix prefixes
// path and whose match.request_lane (if set) equals requestLane. Rules are
// pre-sorted (priority desc, then prefix length desc), so the first hit wins.
func (m *Matcher) Match(path, requestLane string) (MatchResult, bool) {
	if m.snapshot == nil {
		return MatchResult{}, false
	}
	for _, r := range m.snapshot.Rules() {
		if !r.Enabled {
			continue
		}
		if r.Match.RequestLane != "" && r.Match.RequestLane != requestLane {
			continue
		}
		prefix := r.Match.PathPrefix
		if strings.HasPrefix(path, prefix) {
			return MatchResult{Rule: r}, true
		}
		// "/dashboard" should 301 to "/dashboard/".
		if strings.HasSuffix(prefix, "/") && path == strings.TrimSuffix(prefix, "/") {
			return MatchResult{Rule: r, Redirect: true}, true
		}
	}
	return MatchResult{}, false
}

// RewritePath applies strip_prefix and rewrite_prefix to a request path.
func RewritePath(path string, t Target) string {
	if t.StripPrefix == "" {
		return path
	}
	trimmed := strings.TrimPrefix(path, t.StripPrefix)
	return t.RewritePrefix + trimmed
}

// EmergencyRules returns the hardcoded life-saving rules used only at cold start
// (current snapshot is nil, never fetched). They cover just the operator entry
// points so the dashboard / paas-engine stay reachable to diagnose. Business
// paths are intentionally excluded.
func EmergencyRules() []Rule {
	return []Rule{
		{Name: "emergency-paas", Enabled: true, Priority: 100,
			Match:   Match{PathPrefix: "/api/paas/"},
			Targets: []Target{{Service: "paas-engine", Port: 8080, Weight: 100}}},
		{Name: "emergency-dashboard-api", Enabled: true, Priority: 100,
			Match:   Match{PathPrefix: "/dashboard/api/"},
			Targets: []Target{{Service: "monitor-dashboard", Port: 3002, Weight: 100}}},
		{Name: "emergency-dashboard-web", Enabled: true, Priority: 100,
			Match:   Match{PathPrefix: "/dashboard/"},
			Targets: []Target{{Service: "monitor-dashboard-web", Port: 80, Weight: 100}}},
	}
}
