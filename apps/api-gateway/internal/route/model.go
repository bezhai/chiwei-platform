package route

// Match holds the conditions a request must satisfy to select a rule.
// Only PathPrefix and RequestLane are honored in this version; the other
// fields are accepted in the wire schema but rejected by paas-engine's
// validator (second-phase features).
type Match struct {
	PathPrefix  string `json:"path_prefix"`
	RequestLane string `json:"request_lane,omitempty"`
}

// Target is the upstream destination a matched rule forwards to.
// Lane may be empty: empty means "follow the request's x-lane (passthrough)",
// non-empty means "force routing to this lane".
type Target struct {
	Service       string `json:"service"`
	Lane          string `json:"lane,omitempty"`
	Port          int    `json:"port"`
	Weight        int    `json:"weight,omitempty"`
	StripPrefix   string `json:"strip_prefix,omitempty"`
	RewritePrefix string `json:"rewrite_prefix,omitempty"`
}

// Rule is one routing rule.
type Rule struct {
	Name     string   `json:"name"`
	Enabled  bool     `json:"enabled"`
	Priority int      `json:"priority"`
	Match    Match    `json:"match"`
	Targets  []Target `json:"targets"`
	// SplitKeyHeaders is an ordered list of header names used for stable
	// (sticky) target selection: the first present, non-empty header value is
	// hashed with the rule name to pick a target deterministically. Empty means
	// no stable split (weighted-random selection).
	SplitKeyHeaders []string `json:"split_key_headers,omitempty"`
}
