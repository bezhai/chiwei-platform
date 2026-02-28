package route

import "strings"

// Matcher holds routes sorted by prefix length (longest first).
type Matcher struct {
	routes []Route
}

// NewMatcher creates a Matcher from pre-sorted routes.
func NewMatcher(routes []Route) *Matcher {
	return &Matcher{routes: routes}
}

// MatchResult describes the outcome of a route match.
type MatchResult struct {
	Route    Route
	Redirect bool // true = 301 to path with trailing slash
}

// Match returns the first route whose prefix matches the path.
// If the path matches a prefix without its trailing slash (e.g. "/dashboard"
// for prefix "/dashboard/"), it signals a 301 redirect.
func (m *Matcher) Match(path string) (MatchResult, bool) {
	for _, r := range m.routes {
		if strings.HasPrefix(path, r.Prefix) {
			return MatchResult{Route: r}, true
		}
		// "/dashboard" should 301 to "/dashboard/"
		if strings.HasSuffix(r.Prefix, "/") && path == strings.TrimSuffix(r.Prefix, "/") {
			return MatchResult{Route: r, Redirect: true}, true
		}
	}
	return MatchResult{}, false
}

// RewritePath applies strip_prefix and rewrite_prefix to a request path.
func RewritePath(path string, r Route) string {
	if r.StripPrefix == "" {
		return path
	}
	trimmed := strings.TrimPrefix(path, r.StripPrefix)
	return r.RewritePrefix + trimmed
}
