package route

import "sort"

// Snapshot is an immutable, pre-sorted set of routing rules plus the version
// label that produced it. Rules are sorted once at construction so the matcher
// can scan them in priority order without re-sorting per request.
type Snapshot struct {
	version int64
	rules   []Rule
}

// NewSnapshot builds a Snapshot from rules, sorting them by priority desc, then
// path_prefix length desc, so the first matching rule during a linear scan is
// the winner.
func NewSnapshot(version int64, rules []Rule) *Snapshot {
	sorted := make([]Rule, len(rules))
	copy(sorted, rules)
	sort.SliceStable(sorted, func(i, j int) bool {
		if sorted[i].Priority != sorted[j].Priority {
			return sorted[i].Priority > sorted[j].Priority
		}
		return len(sorted[i].Match.PathPrefix) > len(sorted[j].Match.PathPrefix)
	})
	return &Snapshot{version: version, rules: sorted}
}

// Version returns the snapshot version label (for logs/metrics). It is a
// monotonic int64 produced by paas-engine; convert to string when used as a
// metric label.
func (s *Snapshot) Version() int64 { return s.version }

// Rules returns the sorted rules.
func (s *Snapshot) Rules() []Rule { return s.rules }
