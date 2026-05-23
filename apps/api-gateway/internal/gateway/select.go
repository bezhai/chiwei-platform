package gateway

import "github.com/chiwei-platform/api-gateway/internal/route"

// selectTarget picks one target by weighted random. draw is a value in [0,1)
// (typically from the injected random source); it is scaled across the total
// weight and the cumulative-threshold walk picks the matching target. A single
// target always wins. A target with weight 0 is never selected. Targets are
// validated upstream (weights sum to 100, at least one target), so this only
// needs a sane fallback for the degenerate all-zero case.
func selectTarget(targets []route.Target, draw float64) route.Target {
	if len(targets) == 1 {
		return targets[0]
	}
	total := 0
	for _, t := range targets {
		total += t.Weight
	}
	if total <= 0 {
		// Degenerate (all weights zero): fall back to the first target rather
		// than divide by zero or return nothing.
		return targets[0]
	}
	point := draw * float64(total)
	cumulative := 0
	for _, t := range targets {
		cumulative += t.Weight
		if point < float64(cumulative) {
			return t
		}
	}
	// draw is in [0,1) so point < total; the loop returns above. This is an
	// unreachable safety net for floating-point edge cases.
	return targets[len(targets)-1]
}
