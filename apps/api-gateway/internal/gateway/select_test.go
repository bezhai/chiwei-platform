package gateway

import (
	"testing"

	"github.com/chiwei-platform/api-gateway/internal/route"
)

// TestSelectTargetSingle: a single target (weight 100) is always chosen,
// regardless of the random draw.
func TestSelectTargetSingle(t *testing.T) {
	targets := []route.Target{
		{Service: "agent-service", Port: 8000, Weight: 100},
	}
	for _, draw := range []float64{0.0, 0.5, 0.999} {
		got := selectTarget(targets, draw)
		if got.Service != "agent-service" {
			t.Errorf("draw=%v: got %q want agent-service", draw, got.Service)
		}
	}
}

// TestSelectTargetWeightedBoundary: a 90/10 split. The draw is a value in
// [0,1) scaled across the total weight (100). Draws in [0,0.90) pick the
// 90-weight target A; draws in [0.90,1.0) pick the 10-weight target B. We
// assert the exact boundary deterministically (no statistical wobble).
func TestSelectTargetWeightedBoundary(t *testing.T) {
	targets := []route.Target{
		{Service: "svc-a", Lane: "prod", Port: 8000, Weight: 90},
		{Service: "svc-b", Lane: "ppe-new", Port: 8000, Weight: 10},
	}
	cases := []struct {
		draw float64
		want string
	}{
		{0.0, "svc-a"},     // bottom of A's range
		{0.5, "svc-a"},     // middle of A's range
		{0.8999, "svc-a"},  // just below the A/B boundary
		{0.90, "svc-b"},    // exactly at the boundary -> B
		{0.95, "svc-b"},    // middle of B's range
		{0.9999, "svc-b"},  // top of B's range
	}
	for _, c := range cases {
		got := selectTarget(targets, c.draw)
		if got.Service != c.want {
			t.Errorf("draw=%v: got %q want %q", c.draw, got.Service, c.want)
		}
	}
}

// TestSelectTargetZeroWeight: a target with weight 0 is never selected; the
// other target (weight 100) absorbs the whole range. Mirrors the "set weight
// to 0 to drain a target" hemostasis flow.
func TestSelectTargetZeroWeight(t *testing.T) {
	targets := []route.Target{
		{Service: "drained", Port: 8000, Weight: 0},
		{Service: "live", Port: 8000, Weight: 100},
	}
	for _, draw := range []float64{0.0, 0.001, 0.5, 0.9999} {
		got := selectTarget(targets, draw)
		if got.Service != "live" {
			t.Errorf("draw=%v: got %q want live (weight-0 target must never win)", draw, got.Service)
		}
	}
}

// TestSelectTargetDistribution: a large-sample distribution check using a
// deterministic stepping source (not real randomness, so it is not flaky).
// Feeding evenly-spaced draws across [0,1) must split ~90:10 for a 90/10
// rule. We assert exact counts because the source is deterministic.
func TestSelectTargetDistribution(t *testing.T) {
	targets := []route.Target{
		{Service: "svc-a", Port: 8000, Weight: 90},
		{Service: "svc-b", Port: 8000, Weight: 10},
	}
	const n = 10000
	countA, countB := 0, 0
	for i := 0; i < n; i++ {
		draw := float64(i) / float64(n) // evenly spaced over [0,1)
		got := selectTarget(targets, draw)
		switch got.Service {
		case "svc-a":
			countA++
		case "svc-b":
			countB++
		default:
			t.Fatalf("unexpected service %q", got.Service)
		}
	}
	// With evenly-spaced deterministic draws the split is exact: 9000/1000.
	if countA != 9000 || countB != 1000 {
		t.Errorf("distribution: A=%d B=%d want A=9000 B=1000", countA, countB)
	}
}
