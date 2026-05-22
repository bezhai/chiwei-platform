package route

// Fallback modes.
const (
	FallbackProd   = "prod"
	FallbackReject = "reject"
)

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

// Fallback is part of the wire schema (set by paas-engine) describing what
// should happen when the target lane has no instance: "prod" falls back to the
// service's prod instance, "reject" fails closed. The api-gateway no longer
// acts on this — lane resolution and fail-closed are delegated to the
// lane-sidecar — so the field is currently advisory only.
type Fallback struct {
	Mode string `json:"mode"`
}

// Rule is one routing rule.
type Rule struct {
	Name     string   `json:"name"`
	Enabled  bool     `json:"enabled"`
	Priority int      `json:"priority"`
	Match    Match    `json:"match"`
	Targets  []Target `json:"targets"`
	Fallback Fallback `json:"fallback"`
}
