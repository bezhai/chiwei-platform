package domain

import "testing"

// gwRule builds a single-target GatewayRule for explain tests. It mirrors the
// shape api-gateway consumes: top-level PathPrefix/RequestLane are kept equal to
// Match.PathPrefix/Match.RequestLane (paas-engine validation enforces this), and
// the matcher (apps/api-gateway/internal/route/matcher.go) matches on the Match
// fields, so explain must too.
func gwRule(name, prefix, reqLane string, priority int, enabled bool, targets ...GatewayTarget) *GatewayRule {
	return &GatewayRule{
		Name:        name,
		Enabled:     enabled,
		Priority:    priority,
		PathPrefix:  prefix,
		RequestLane: reqLane,
		Match:       GatewayMatch{PathPrefix: prefix, RequestLane: reqLane},
		Targets:     targets,
	}
}

func tgt(service, lane string, port, weight int) GatewayTarget {
	return GatewayTarget{Service: service, Lane: lane, Port: port, Weight: weight}
}

// TestExplainGoldenCases anchors explain's "which rule wins / is it a redirect"
// conclusion against the api-gateway matcher (matcher.go Match). Each expected
// value below was derived by hand-tracing matcher.go and is annotated with the
// matcher line it exercises. matcher.go scans snapshot rules pre-sorted by
// (priority desc, Match.PathPrefix len desc) [snapshot.go NewSnapshot] and
// returns the FIRST rule that is (a) Enabled, (b) Match.RequestLane=="" or
// ==requestLane, (c) strings.HasPrefix(path, prefix) -> hit, or
// (d) prefix ends "/" && path==TrimSuffix(prefix,"/") -> hit+Redirect.
func TestExplainGoldenCases(t *testing.T) {
	cases := []struct {
		name        string
		rules       []*GatewayRule
		path        string
		lane        string
		wantMatched bool
		wantWinner  string
		wantRedir   bool
	}{
		{
			// matcher.go L39 strings.HasPrefix: "/api/agent/health" has prefix
			// "/api/agent/" -> hit, not redirect.
			name: "plain-prefix-hit",
			rules: []*GatewayRule{
				gwRule("agent", "/api/agent/", "", 100, true, tgt("agent-service", "", 8000, 100)),
			},
			path: "/api/agent/health", lane: "",
			wantMatched: true, wantWinner: "agent", wantRedir: false,
		},
		{
			// matcher.go L43 trailing-slash redirect: prefix "/dashboard/" ends
			// in "/" and path "/dashboard" == TrimSuffix("/dashboard/","/")
			// -> hit with Redirect=true.
			name: "trailing-slash-redirect",
			rules: []*GatewayRule{
				gwRule("dash", "/dashboard/", "", 100, true, tgt("monitor-dashboard-web", "", 80, 100)),
			},
			path: "/dashboard", lane: "",
			wantMatched: true, wantWinner: "dash", wantRedir: true,
		},
		{
			// matcher.go L32 !r.Enabled continue: higher-priority rule is
			// disabled and skipped; the enabled lower-priority rule wins.
			name: "disabled-skipped",
			rules: []*GatewayRule{
				gwRule("disabled", "/api/paas/", "", 200, false, tgt("old", "", 1, 100)),
				gwRule("enabled", "/api/paas/", "", 100, true, tgt("paas-engine", "", 8080, 100)),
			},
			path: "/api/paas/apps", lane: "",
			wantMatched: true, wantWinner: "enabled", wantRedir: false,
		},
		{
			// matcher.go L35 request_lane filter: laned rule requires ppe-x and
			// request lane is ppe-x -> laned (priority 200) wins.
			name: "request-lane-match",
			rules: []*GatewayRule{
				gwRule("laned", "/api/paas/", "ppe-x", 200, true, tgt("laned-svc", "ppe-x", 8080, 100)),
				gwRule("generic", "/api/paas/", "", 100, true, tgt("paas-engine", "", 8080, 100)),
			},
			path: "/api/paas/x", lane: "ppe-x",
			wantMatched: true, wantWinner: "laned", wantRedir: false,
		},
		{
			// matcher.go L35 request_lane filter: laned rule requires ppe-x but
			// request lane is ppe-other -> laned skipped, generic wins.
			name: "request-lane-mismatch-falls-through",
			rules: []*GatewayRule{
				gwRule("laned", "/api/paas/", "ppe-x", 200, true, tgt("laned-svc", "ppe-x", 8080, 100)),
				gwRule("generic", "/api/paas/", "", 100, true, tgt("paas-engine", "", 8080, 100)),
			},
			path: "/api/paas/x", lane: "ppe-other",
			wantMatched: true, wantWinner: "generic", wantRedir: false,
		},
		{
			// snapshot.go NewSnapshot sort: same priority, longer Match prefix
			// sorts first -> "/dashboard/api/" (len 15) wins over "/dashboard/"
			// (len 11) for path under /dashboard/api/.
			name: "same-priority-longer-prefix-wins",
			rules: []*GatewayRule{
				gwRule("short", "/dashboard/", "", 100, true, tgt("web", "", 80, 100)),
				gwRule("long", "/dashboard/api/", "", 100, true, tgt("monitor-dashboard", "", 3002, 100)),
			},
			path: "/dashboard/api/metrics", lane: "",
			wantMatched: true, wantWinner: "long", wantRedir: false,
		},
		{
			// matcher.go L31-47 no rule prefixes the path -> no match.
			name: "no-match",
			rules: []*GatewayRule{
				gwRule("agent", "/api/agent/", "", 100, true, tgt("agent-service", "", 8000, 100)),
			},
			path: "/totally/unknown", lane: "",
			wantMatched: false, wantWinner: "", wantRedir: false,
		},
		{
			// priority desc: higher priority rule with same prefix wins
			// (matcher.go relies on snapshot.go sort, priority before length).
			name: "higher-priority-wins",
			rules: []*GatewayRule{
				gwRule("low", "/api/paas/", "", 100, true, tgt("old-svc", "", 1, 100)),
				gwRule("high", "/api/paas/", "", 200, true, tgt("new-svc", "", 2, 100)),
			},
			path: "/api/paas/x", lane: "",
			wantMatched: true, wantWinner: "high", wantRedir: false,
		},
		{
			// snapshot.go NewSnapshot sort tiebreak: when priority AND
			// Match.PathPrefix length are equal, fall back to name asc. Input
			// order is [bbb, aaa] on purpose — the winner must be decided by the
			// sort (name asc -> "aaa"), not by input order. This pins the
			// tiebreak api-gateway inherits via paas-engine snapshot ordering;
			// if List/Snapshot sorting drifts, this case catches it.
			name: "same-priority-same-length-name-asc-wins",
			rules: []*GatewayRule{
				gwRule("bbb", "/api/paas/", "", 100, true, tgt("svc-b", "", 8080, 100)),
				gwRule("aaa", "/api/paas/", "", 100, true, tgt("svc-a", "", 8080, 100)),
			},
			path: "/api/paas/x", lane: "",
			wantMatched: true, wantWinner: "aaa", wantRedir: false,
		},
	}

	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			res := ExplainGatewayMatch(c.rules, c.path, c.lane)
			if res.Matched != c.wantMatched {
				t.Fatalf("matched=%v want %v", res.Matched, c.wantMatched)
			}
			if c.wantMatched {
				if res.WinningRule != c.wantWinner {
					t.Errorf("winner=%q want %q", res.WinningRule, c.wantWinner)
				}
				if res.WouldRedirect != c.wantRedir {
					t.Errorf("would_redirect=%v want %v", res.WouldRedirect, c.wantRedir)
				}
			}
		})
	}
}

// TestExplainNonMatchReasons covers the per-rule non-match classification:
// disabled, request_lane mismatch, path prefix mismatch, and shadowed (the rule
// itself would have matched but a higher-priority rule won first).
func TestExplainNonMatchReasons(t *testing.T) {
	rules := []*GatewayRule{
		gwRule("winner", "/api/", "", 200, true, tgt("svc-a", "", 80, 100)),
		gwRule("shadowed", "/api/", "", 100, true, tgt("svc-b", "", 80, 100)),
		gwRule("disabled", "/api/", "", 300, false, tgt("svc-c", "", 80, 100)),
		gwRule("wrong-lane", "/api/", "ppe-x", 250, true, tgt("svc-d", "ppe-x", 80, 100)),
		gwRule("wrong-path", "/other/", "", 150, true, tgt("svc-e", "", 80, 100)),
	}
	res := ExplainGatewayMatch(rules, "/api/thing", "")

	if res.WinningRule != "winner" {
		t.Fatalf("winner=%q want winner", res.WinningRule)
	}

	byName := map[string]GatewayRuleExplain{}
	for _, e := range res.Rules {
		byName[e.Name] = e
	}

	checks := []struct {
		rule       string
		wantStatus string
	}{
		{"winner", ExplainStatusWinner},
		{"shadowed", ExplainStatusShadowed},
		{"disabled", ExplainStatusDisabled},
		{"wrong-lane", ExplainStatusLaneMismatch},
		{"wrong-path", ExplainStatusPathMismatch},
	}
	for _, c := range checks {
		e, ok := byName[c.rule]
		if !ok {
			t.Fatalf("rule %q missing from explain", c.rule)
		}
		if e.Status != c.wantStatus {
			t.Errorf("rule %q status=%q want %q (reason=%q)", c.rule, e.Status, c.wantStatus, e.Reason)
		}
	}
}

// TestExplainEffectiveLane verifies effective-lane resolution: a target with a
// non-empty lane overrides the request x-lane; an empty target lane follows the
// request x-lane.
func TestExplainEffectiveLane(t *testing.T) {
	// target lane set -> overrides request lane
	rules := []*GatewayRule{
		gwRule("forced", "/api/", "", 100, true, tgt("svc", "ppe-new", 80, 100)),
	}
	res := ExplainGatewayMatch(rules, "/api/x", "prod")
	if len(res.CandidateTargets) != 1 {
		t.Fatalf("expected 1 candidate, got %d", len(res.CandidateTargets))
	}
	if res.CandidateTargets[0].EffectiveLane != "ppe-new" {
		t.Errorf("forced lane: effective=%q want ppe-new", res.CandidateTargets[0].EffectiveLane)
	}

	// empty target lane -> follows request lane
	rules = []*GatewayRule{
		gwRule("follow", "/api/", "", 100, true, tgt("svc", "", 80, 100)),
	}
	res = ExplainGatewayMatch(rules, "/api/x", "prod")
	if res.CandidateTargets[0].EffectiveLane != "prod" {
		t.Errorf("follow lane: effective=%q want prod", res.CandidateTargets[0].EffectiveLane)
	}
}
