package route

import "testing"

// helper to build a rule with a single target
func rule(name, prefix, reqLane string, priority int, enabled bool, t Target) Rule {
	return Rule{
		Name:     name,
		Enabled:  enabled,
		Priority: priority,
		Match:    Match{PathPrefix: prefix, RequestLane: reqLane},
		Targets:  []Target{t},
	}
}

func TestMatchLongestPrefixAndPriority(t *testing.T) {
	rules := []Rule{
		rule("dash-api", "/dashboard/api/", "", 100, true, Target{Service: "monitor-dashboard", Port: 3002}),
		rule("dash-web", "/dashboard/", "", 100, true, Target{Service: "monitor-dashboard-web", Port: 80}),
		rule("paas", "/api/paas/", "", 100, true, Target{Service: "paas-engine", Port: 8080}),
		rule("webhook", "/webhook/", "", 100, true, Target{Service: "channel-proxy", Port: 3003}),
	}
	m := NewMatcher(NewSnapshot(1, rules))

	tests := []struct {
		path    string
		service string
		ok      bool
	}{
		{"/dashboard/api/metrics", "monitor-dashboard", true},
		{"/dashboard/index.html", "monitor-dashboard-web", true},
		{"/api/paas/apps/", "paas-engine", true},
		{"/webhook/bot1/event", "channel-proxy", true},
		{"/unknown/path", "", false},
	}
	for _, tt := range tests {
		res, ok := m.Match(tt.path, "")
		if ok != tt.ok {
			t.Errorf("Match(%q): ok=%v want %v", tt.path, ok, tt.ok)
			continue
		}
		if ok && res.Rule.Targets[0].Service != tt.service {
			t.Errorf("Match(%q): service=%q want %q", tt.path, res.Rule.Targets[0].Service, tt.service)
		}
	}
}

func TestMatchPriorityWins(t *testing.T) {
	// Two rules same path_prefix, different priority: higher priority wins.
	rules := []Rule{
		rule("low", "/api/paas/", "", 100, true, Target{Service: "old-svc", Port: 1}),
		rule("high", "/api/paas/", "", 200, true, Target{Service: "new-svc", Port: 2}),
	}
	m := NewMatcher(NewSnapshot(1, rules))
	res, ok := m.Match("/api/paas/x", "")
	if !ok {
		t.Fatal("expected match")
	}
	if res.Rule.Targets[0].Service != "new-svc" {
		t.Errorf("priority: got %q want new-svc", res.Rule.Targets[0].Service)
	}
}

func TestMatchSamePriorityLongerPrefixWins(t *testing.T) {
	rules := []Rule{
		rule("short", "/dashboard/", "", 100, true, Target{Service: "web", Port: 80}),
		rule("long", "/dashboard/api/", "", 100, true, Target{Service: "api", Port: 3002}),
	}
	m := NewMatcher(NewSnapshot(1, rules))
	res, ok := m.Match("/dashboard/api/metrics", "")
	if !ok {
		t.Fatal("expected match")
	}
	if res.Rule.Targets[0].Service != "api" {
		t.Errorf("same-priority longer prefix: got %q want api", res.Rule.Targets[0].Service)
	}
}

func TestMatchDisabledSkipped(t *testing.T) {
	rules := []Rule{
		rule("disabled", "/api/paas/", "", 200, false, Target{Service: "disabled-svc", Port: 1}),
		rule("enabled", "/api/paas/", "", 100, true, Target{Service: "enabled-svc", Port: 8080}),
	}
	m := NewMatcher(NewSnapshot(1, rules))
	res, ok := m.Match("/api/paas/x", "")
	if !ok {
		t.Fatal("expected match")
	}
	if res.Rule.Targets[0].Service != "enabled-svc" {
		t.Errorf("disabled skip: got %q want enabled-svc", res.Rule.Targets[0].Service)
	}
}

func TestMatchRequestLaneFilter(t *testing.T) {
	rules := []Rule{
		// rule requires request_lane == ppe-x
		rule("laned", "/api/paas/", "ppe-x", 200, true, Target{Service: "laned-svc", Port: 1}),
		// generic rule, no lane constraint
		rule("generic", "/api/paas/", "", 100, true, Target{Service: "generic-svc", Port: 8080}),
	}
	m := NewMatcher(NewSnapshot(1, rules))

	// request_lane matches the laned rule -> higher priority wins
	res, ok := m.Match("/api/paas/x", "ppe-x")
	if !ok || res.Rule.Targets[0].Service != "laned-svc" {
		t.Errorf("request_lane match: ok=%v svc=%q", ok, res.Rule.Targets[0].Service)
	}

	// request_lane does NOT match laned rule -> falls to generic
	res, ok = m.Match("/api/paas/x", "ppe-other")
	if !ok || res.Rule.Targets[0].Service != "generic-svc" {
		t.Errorf("request_lane mismatch: ok=%v svc=%q", ok, res.Rule.Targets[0].Service)
	}

	// empty request_lane -> only generic matches
	res, ok = m.Match("/api/paas/x", "")
	if !ok || res.Rule.Targets[0].Service != "generic-svc" {
		t.Errorf("empty request_lane: ok=%v svc=%q", ok, res.Rule.Targets[0].Service)
	}
}

func TestMatchNoRules(t *testing.T) {
	m := NewMatcher(NewSnapshot(1, nil))
	if _, ok := m.Match("/anything", ""); ok {
		t.Error("expected no match on empty snapshot")
	}
}

func TestRewritePath(t *testing.T) {
	tests := []struct {
		path   string
		target Target
		expect string
	}{
		{"/api/paas/apps/", Target{}, "/api/paas/apps/"},
		{"/api/agent/health", Target{StripPrefix: "/api/agent"}, "/health"},
		{"/dashboard/api/metrics", Target{StripPrefix: "/dashboard/api", RewritePrefix: "/dashboard"}, "/dashboard/metrics"},
		{"/webhook/bot1/event", Target{}, "/webhook/bot1/event"},
	}
	for _, tt := range tests {
		got := RewritePath(tt.path, tt.target)
		if got != tt.expect {
			t.Errorf("RewritePath(%q): got %q want %q", tt.path, got, tt.expect)
		}
	}
}
