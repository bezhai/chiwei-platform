package gateway

import (
	"testing"

	"github.com/chiwei-platform/api-gateway/internal/route"
)

// TestSelectTargetStableBoundary: stable selection maps a 0-99 bucket onto the
// cumulative weight ranges. For a 90/10 split, buckets [0,90) pick A and
// [90,100) pick B. We assert exact boundaries deterministically.
func TestSelectTargetStableBoundary(t *testing.T) {
	targets := []route.Target{
		{Service: "svc-a", Lane: "prod", Port: 8000, Weight: 90},
		{Service: "svc-b", Lane: "ppe-new", Port: 8000, Weight: 10},
	}
	cases := []struct {
		bucket int
		want   string
	}{
		{0, "svc-a"},
		{50, "svc-a"},
		{89, "svc-a"},
		{90, "svc-b"},
		{95, "svc-b"},
		{99, "svc-b"},
	}
	for _, c := range cases {
		got := selectTargetStable(targets, c.bucket)
		if got.Service != c.want {
			t.Errorf("bucket=%d: got %q want %q", c.bucket, got.Service, c.want)
		}
	}
}

// TestSelectTargetStableSingle: a single target always wins regardless of bucket.
func TestSelectTargetStableSingle(t *testing.T) {
	targets := []route.Target{{Service: "only", Port: 8000, Weight: 100}}
	for _, b := range []int{0, 33, 99} {
		if got := selectTargetStable(targets, b); got.Service != "only" {
			t.Errorf("bucket=%d: got %q want only", b, got.Service)
		}
	}
}

// TestSelectTargetStableZeroWeight: a weight-0 target is never selected.
func TestSelectTargetStableZeroWeight(t *testing.T) {
	targets := []route.Target{
		{Service: "drained", Port: 8000, Weight: 0},
		{Service: "live", Port: 8000, Weight: 100},
	}
	for _, b := range []int{0, 1, 50, 99} {
		if got := selectTargetStable(targets, b); got.Service != "live" {
			t.Errorf("bucket=%d: got %q want live (weight-0 must never win)", b, got.Service)
		}
	}
}

// TestSelectTargetStableFullSwitch: weights 100/0 send every bucket to A;
// flipping to 0/100 sends every bucket to B (调权后立即全切).
func TestSelectTargetStableFullSwitch(t *testing.T) {
	allA := []route.Target{
		{Service: "a", Port: 8000, Weight: 100},
		{Service: "b", Port: 8000, Weight: 0},
	}
	allB := []route.Target{
		{Service: "a", Port: 8000, Weight: 0},
		{Service: "b", Port: 8000, Weight: 100},
	}
	for b := 0; b < 100; b++ {
		if got := selectTargetStable(allA, b); got.Service != "a" {
			t.Errorf("100/0 bucket=%d: got %q want a", b, got.Service)
		}
		if got := selectTargetStable(allB, b); got.Service != "b" {
			t.Errorf("0/100 bucket=%d: got %q want b", b, got.Service)
		}
	}
}

// TestStableBucket: the bucket is hash(rule_name + key) % 100; the same
// (rule, key) pair always lands in the same bucket, and changing either input
// changes the input fed to the hash. We use an injectable hash so the test is
// deterministic and independent of the production hash algorithm.
func TestStableBucket(t *testing.T) {
	// Identity hash over the byte sum keeps the math obvious and deterministic.
	h := func(s string) uint64 {
		var sum uint64
		for _, c := range []byte(s) {
			sum += uint64(c)
		}
		return sum
	}
	b1 := stableBucket(h, "rule-x", "user-1")
	b2 := stableBucket(h, "rule-x", "user-1")
	if b1 != b2 {
		t.Errorf("same (rule,key) must be stable: %d vs %d", b1, b2)
	}
	if b1 < 0 || b1 > 99 {
		t.Errorf("bucket %d out of [0,99]", b1)
	}
	// Different rule name with same key must feed a different string to the
	// hash (so two rules don't share a sticky bucket by accident).
	if stableBucket(h, "rule-x", "k") == stableBucket(h, "rule-y", "k") {
		// With the identity hash this would only collide if "rule-x"+key and
		// "rule-y"+key sum equal; they don't, so this asserts the name is mixed in.
		t.Errorf("rule name must be part of the hashed string")
	}
}

// TestResolveSplitKey covers the key-resolution boundaries the spec mandates:
// case-insensitive header lookup, first-present-in-order wins, empty value
// counts as absent, multi-value header takes the first value, and no headers /
// no match returns absent.
func TestResolveSplitKey(t *testing.T) {
	cases := []struct {
		name       string
		headerKeys []string
		headers    map[string][]string
		wantKey    string
		wantOK     bool
	}{
		{
			name:       "no split headers configured",
			headerKeys: nil,
			headers:    map[string][]string{"X-User-Id": {"u1"}},
			wantKey:    "", wantOK: false,
		},
		{
			name:       "case-insensitive lookup",
			headerKeys: []string{"x-user-id"},
			headers:    map[string][]string{"X-User-Id": {"u1"}},
			wantKey:    "u1", wantOK: true,
		},
		{
			name:       "first present in order wins",
			headerKeys: []string{"X-Trace-Id", "X-User-Id"},
			headers:    map[string][]string{"X-User-Id": {"u1"}},
			wantKey:    "u1", wantOK: true,
		},
		{
			name:       "earlier header preferred when both present",
			headerKeys: []string{"X-Trace-Id", "X-User-Id"},
			headers:    map[string][]string{"X-Trace-Id": {"t1"}, "X-User-Id": {"u1"}},
			wantKey:    "t1", wantOK: true,
		},
		{
			name:       "empty value counts as absent, falls to next header",
			headerKeys: []string{"X-Trace-Id", "X-User-Id"},
			headers:    map[string][]string{"X-Trace-Id": {""}, "X-User-Id": {"u1"}},
			wantKey:    "u1", wantOK: true,
		},
		{
			name:       "all empty -> absent",
			headerKeys: []string{"X-Trace-Id"},
			headers:    map[string][]string{"X-Trace-Id": {""}},
			wantKey:    "", wantOK: false,
		},
		{
			name:       "multi-value header takes first value",
			headerKeys: []string{"X-User-Id"},
			headers:    map[string][]string{"X-User-Id": {"first", "second"}},
			wantKey:    "first", wantOK: true,
		},
		{
			name:       "no matching header -> absent",
			headerKeys: []string{"X-User-Id"},
			headers:    map[string][]string{"X-Other": {"x"}},
			wantKey:    "", wantOK: false,
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := make(map[string][]string)
			for k, v := range c.headers {
				h[k] = v
			}
			gotKey, gotOK := resolveSplitKey(c.headerKeys, headerGetter(h))
			if gotOK != c.wantOK {
				t.Fatalf("ok=%v want %v (key=%q)", gotOK, c.wantOK, gotKey)
			}
			if gotKey != c.wantKey {
				t.Errorf("key=%q want %q", gotKey, c.wantKey)
			}
		})
	}
}
