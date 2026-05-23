package domain

import (
	"sort"
	"strings"
)

// Explain status values for each rule in an explain trace.
const (
	// ExplainStatusWinner: this rule is the one api-gateway would select.
	ExplainStatusWinner = "winner"
	// ExplainStatusShadowed: this rule would have matched the request, but a
	// higher-priority rule (earlier in sort order) won first.
	ExplainStatusShadowed = "shadowed"
	// ExplainStatusDisabled: rule is disabled, matcher skips it entirely.
	ExplainStatusDisabled = "disabled"
	// ExplainStatusLaneMismatch: rule constrains request_lane and it differs
	// from the request's x-lane.
	ExplainStatusLaneMismatch = "request_lane_mismatch"
	// ExplainStatusPathMismatch: the request path is not under this rule's
	// path_prefix (and is not the trailing-slash redirect case).
	ExplainStatusPathMismatch = "path_prefix_mismatch"
)

// GatewayExplainTarget describes one candidate target of the winning rule plus
// the effective lane that would apply to a request routed through it.
type GatewayExplainTarget struct {
	Service string `json:"service"`
	Lane    string `json:"lane"`
	Port    int    `json:"port"`
	Weight  int    `json:"weight"`
	// EffectiveLane is the lane api-gateway would stamp into X-Ctx-Lane:
	// target.lane if non-empty (override), else the request's x-lane (follow).
	EffectiveLane string `json:"effective_lane"`
}

// GatewayRuleExplain is the per-rule verdict in an explain trace.
type GatewayRuleExplain struct {
	Name        string `json:"name"`
	Priority    int    `json:"priority"`
	PathPrefix  string `json:"path_prefix"`
	RequestLane string `json:"request_lane,omitempty"`
	Enabled     bool   `json:"enabled"`
	// Status is one of the ExplainStatus* constants.
	Status string `json:"status"`
	// Reason is a human-readable sentence explaining the status.
	Reason string `json:"reason"`
}

// GatewayExplainResult is the full trace for one (path, request_lane) probe.
type GatewayExplainResult struct {
	Path        string `json:"path"`
	RequestLane string `json:"request_lane,omitempty"`
	Matched     bool   `json:"matched"`
	// WinningRule is the name of the selected rule, empty when Matched is false.
	WinningRule string `json:"winning_rule,omitempty"`
	// WinningReason explains why the winning rule was selected.
	WinningReason string `json:"winning_reason,omitempty"`
	// WouldForward / WouldRedirect are mutually exclusive and only meaningful
	// when Matched. WouldRedirect mirrors the matcher's trailing-slash 301.
	WouldForward  bool `json:"would_forward"`
	WouldRedirect bool `json:"would_redirect"`
	// CandidateTargets are the winning rule's targets with effective lanes.
	CandidateTargets []GatewayExplainTarget `json:"candidate_targets,omitempty"`
	// EffectiveLaneNote explains how effective lane is derived.
	EffectiveLaneNote string `json:"effective_lane_note,omitempty"`
	// Rules is every rule's verdict, in matcher sort order.
	Rules []GatewayRuleExplain `json:"rules"`
}

// ExplainGatewayMatch replicates api-gateway's matcher (matcher.go Match over a
// Snapshot built by snapshot.go NewSnapshot) as a pure function that, instead of
// returning only the first hit, annotates every rule with why it did or did not
// win. The winning rule / redirect verdict is byte-for-byte aligned with the
// matcher; see gateway_explain_test.go golden cases.
//
// Sort order (matches NewSnapshot + List): priority desc, then Match.PathPrefix
// length desc, then name asc for stability. matcher.go matches on the Match
// fields (Match.PathPrefix / Match.RequestLane), so this does too.
func ExplainGatewayMatch(rules []*GatewayRule, path, requestLane string) GatewayExplainResult {
	sorted := make([]*GatewayRule, len(rules))
	copy(sorted, rules)
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].Priority != sorted[j].Priority {
			return sorted[i].Priority > sorted[j].Priority
		}
		li, lj := len(sorted[i].Match.PathPrefix), len(sorted[j].Match.PathPrefix)
		if li != lj {
			return li > lj
		}
		return sorted[i].Name < sorted[j].Name
	})

	result := GatewayExplainResult{
		Path:        path,
		RequestLane: requestLane,
		Rules:       make([]GatewayRuleExplain, 0, len(sorted)),
	}

	winnerFound := false
	for _, r := range sorted {
		entry := GatewayRuleExplain{
			Name:        r.Name,
			Priority:    r.Priority,
			PathPrefix:  r.Match.PathPrefix,
			RequestLane: r.Match.RequestLane,
			Enabled:     r.Enabled,
		}

		hit, redirect := ruleHits(r, path, requestLane)

		switch {
		case !r.Enabled:
			// matcher.go L32: disabled rules are skipped before any other check.
			entry.Status = ExplainStatusDisabled
			entry.Reason = "rule is disabled, matcher skips it"
		case r.Match.RequestLane != "" && r.Match.RequestLane != requestLane:
			// matcher.go L35: request_lane constraint not satisfied.
			entry.Status = ExplainStatusLaneMismatch
			entry.Reason = "rule requires request_lane=" + quote(r.Match.RequestLane) +
				" but request x-lane=" + quote(requestLane)
		case !hit:
			// matcher.go L39/L43: path is not under the prefix (nor the
			// trailing-slash redirect of it).
			entry.Status = ExplainStatusPathMismatch
			entry.Reason = "path " + quote(path) + " is not under path_prefix " + quote(r.Match.PathPrefix)
		case !winnerFound:
			// First enabled, lane-ok, path-matching rule in sort order wins.
			winnerFound = true
			entry.Status = ExplainStatusWinner
			if redirect {
				entry.Reason = "path equals path_prefix without trailing slash; matcher issues a 301 redirect"
				result.WouldRedirect = true
			} else {
				entry.Reason = "highest-priority enabled rule whose path_prefix prefixes the request path"
				result.WouldForward = true
			}
			result.Matched = true
			result.WinningRule = r.Name
			result.WinningReason = entry.Reason
			result.CandidateTargets = buildCandidates(r, requestLane)
			result.EffectiveLaneNote = effectiveLaneNote(r, requestLane)
		default:
			// Would have matched, but a higher-priority rule already won.
			entry.Status = ExplainStatusShadowed
			entry.Reason = "would have matched, but higher-priority rule " +
				quote(result.WinningRule) + " was selected first"
		}

		result.Rules = append(result.Rules, entry)
	}

	return result
}

// ruleHits reports whether a rule matches the path (ignoring enabled/lane, which
// the caller checks separately) and whether the hit is the trailing-slash
// redirect case. Mirrors matcher.go L38-45.
func ruleHits(r *GatewayRule, path, _ string) (hit, redirect bool) {
	prefix := r.Match.PathPrefix
	if strings.HasPrefix(path, prefix) {
		return true, false
	}
	if strings.HasSuffix(prefix, "/") && path == strings.TrimSuffix(prefix, "/") {
		return true, true
	}
	return false, false
}

func buildCandidates(r *GatewayRule, requestLane string) []GatewayExplainTarget {
	out := make([]GatewayExplainTarget, 0, len(r.Targets))
	for _, t := range r.Targets {
		eff := t.Lane
		if eff == "" {
			eff = requestLane
		}
		out = append(out, GatewayExplainTarget{
			Service:       t.Service,
			Lane:          t.Lane,
			Port:          t.Port,
			Weight:        t.Weight,
			EffectiveLane: eff,
		})
	}
	return out
}

func effectiveLaneNote(r *GatewayRule, requestLane string) string {
	hasForced := false
	for _, t := range r.Targets {
		if t.Lane != "" {
			hasForced = true
			break
		}
	}
	if hasForced {
		return "targets with a non-empty lane override the request x-lane; " +
			"targets with empty lane follow the request x-lane=" + quote(requestLane)
	}
	return "all targets have empty lane, so effective lane follows the request x-lane=" + quote(requestLane)
}

func quote(s string) string {
	if s == "" {
		return `""`
	}
	return `"` + s + `"`
}
