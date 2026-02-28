package route

import "testing"

func newTestMatcher() *Matcher {
	routes := []Route{
		{Prefix: "/dashboard/api/", Service: "monitor-dashboard", Port: 3002, StripPrefix: "/dashboard/api", RewritePrefix: "/dashboard"},
		{Prefix: "/dashboard/", Service: "monitor-dashboard-web", Port: 80},
		{Prefix: "/api/paas/", Service: "paas-engine", Port: 8080, StripPrefix: "/api/paas", RewritePrefix: "/api/v1"},
		{Prefix: "/webhook/", Service: "lark-proxy", Port: 3003},
	}
	sortRoutes(routes)
	return NewMatcher(routes)
}

func TestMatchLongestPrefix(t *testing.T) {
	m := newTestMatcher()

	tests := []struct {
		path     string
		service  string
		ok       bool
		redirect bool
	}{
		{"/dashboard/api/metrics", "monitor-dashboard", true, false},
		{"/dashboard/index.html", "monitor-dashboard-web", true, false},
		{"/api/paas/apps/", "paas-engine", true, false},
		{"/webhook/bot1/event", "lark-proxy", true, false},
		{"/unknown/path", "", false, false},
		{"/dashboard", "monitor-dashboard-web", true, true},
		{"/webhook", "lark-proxy", true, true},
		{"/api/paas", "paas-engine", true, true},
	}

	for _, tt := range tests {
		result, ok := m.Match(tt.path)
		if ok != tt.ok {
			t.Errorf("Match(%q): got ok=%v, want %v", tt.path, ok, tt.ok)
			continue
		}
		if ok && result.Route.Service != tt.service {
			t.Errorf("Match(%q): got service=%q, want %q", tt.path, result.Route.Service, tt.service)
		}
		if ok && result.Redirect != tt.redirect {
			t.Errorf("Match(%q): got redirect=%v, want %v", tt.path, result.Redirect, tt.redirect)
		}
	}
}

func TestRewritePath(t *testing.T) {
	tests := []struct {
		path   string
		route  Route
		expect string
	}{
		{
			"/api/paas/apps/",
			Route{StripPrefix: "/api/paas", RewritePrefix: "/api/v1"},
			"/api/v1/apps/",
		},
		{
			"/dashboard/api/metrics",
			Route{StripPrefix: "/dashboard/api", RewritePrefix: "/dashboard"},
			"/dashboard/metrics",
		},
		{
			"/webhook/bot1/event",
			Route{},
			"/webhook/bot1/event",
		},
	}

	for _, tt := range tests {
		got := RewritePath(tt.path, tt.route)
		if got != tt.expect {
			t.Errorf("RewritePath(%q): got %q, want %q", tt.path, got, tt.expect)
		}
	}
}
