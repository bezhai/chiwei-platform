package gateway

import (
	"net/http"

	"github.com/chiwei-platform/api-gateway/internal/route"
)

// hashFunc maps a string to a uint64. Injectable so tests can drive
// deterministic bucket placement; production uses fnv (see New / forward).
type hashFunc func(string) uint64

// headerGetter resolves a header name to its values, case-insensitively, the
// same way net/http does. It is a thin adapter so resolveSplitKey can be unit
// tested against a plain map without constructing a full *http.Request.
type headerGetter map[string][]string

// Values returns all values for the given header name (canonicalized), or nil.
func (h headerGetter) Values(name string) []string {
	return http.Header(h).Values(name)
}

// keyValuer is the minimal header surface stable splitting needs: ordered
// values for a (canonicalized) header name. *http.Header and headerGetter both
// satisfy it.
type keyValuer interface {
	Values(name string) []string
}

// resolveSplitKey walks splitKeyHeaders in order and returns the first
// non-empty value found. Header lookup is case-insensitive (net/http
// canonicalization); a multi-value header yields its first value; an empty
// string value counts as absent and the walk continues. Returns ("", false)
// when no usable key is found (caller falls back to weighted random).
func resolveSplitKey(splitKeyHeaders []string, h keyValuer) (string, bool) {
	for _, name := range splitKeyHeaders {
		vals := h.Values(name)
		if len(vals) == 0 {
			continue
		}
		if v := vals[0]; v != "" {
			return v, true
		}
	}
	return "", false
}

// stableBucket maps (ruleName, key) to a deterministic bucket in [0,99] via the
// injected hash. ruleName is mixed in with a separator so two different rules
// never share a sticky bucket for the same key by accident.
func stableBucket(hash hashFunc, ruleName, key string) int {
	return int(hash(ruleName+"\x00"+key) % 100)
}

// selectTargetStable picks a target by walking cumulative weights against a
// fixed bucket in [0,99]. Same contract as selectTarget but deterministic: a
// single target always wins, a weight-0 target is never selected, and the
// all-zero degenerate case falls back to the first target.
func selectTargetStable(targets []route.Target, bucket int) route.Target {
	if len(targets) == 1 {
		return targets[0]
	}
	total := 0
	for _, t := range targets {
		total += t.Weight
	}
	if total <= 0 {
		return targets[0]
	}
	// Scale the 0-99 bucket onto the total weight so non-100 sums still work.
	point := bucket * total / 100
	cumulative := 0
	for _, t := range targets {
		cumulative += t.Weight
		if point < cumulative {
			return t
		}
	}
	return targets[len(targets)-1]
}
