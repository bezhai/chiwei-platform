package service

import (
	"context"
	"fmt"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// EnableChange is the before/after audit payload for disable/enable.
type EnableChange struct {
	Name          string `json:"name"`
	BeforeEnabled bool   `json:"before_enabled"`
	AfterEnabled  bool   `json:"after_enabled"`
	Reason        string `json:"reason"`
	Version       int64  `json:"version"`
}

// TargetWeight identifies a target by service+lane and gives its new weight.
// The (service, lane) pair is the target identity used by set-weights.
type TargetWeight struct {
	Service string `json:"service"`
	Lane    string `json:"lane"`
	Weight  int    `json:"weight"`
}

// SetWeightsRequest is the body for the set-weights emergency op. It must list
// every target of the rule exactly once (matched by service+lane); the weights
// wholesale replace the rule's current weights.
type SetWeightsRequest struct {
	Reason  string         `json:"reason"`
	Weights []TargetWeight `json:"weights"`
}

// WeightsChange is the before/after audit payload for set-weights.
type WeightsChange struct {
	Name          string                 `json:"name"`
	BeforeTargets []domain.GatewayTarget `json:"before_targets"`
	AfterTargets  []domain.GatewayTarget `json:"after_targets"`
	Reason        string                 `json:"reason"`
	Version       int64                  `json:"version"`
}

// Disable flips a rule's enabled flag to false, bumps its version, and returns
// the before/after enabled values for auditing. It does NOT re-validate the
// whole rule (止血动作要原子、不重跑整条校验).
func (s *GatewayRuleService) Disable(ctx context.Context, name, reason string) (*EnableChange, error) {
	return s.setEnabled(ctx, name, false, reason)
}

// Enable flips a rule's enabled flag to true.
func (s *GatewayRuleService) Enable(ctx context.Context, name, reason string) (*EnableChange, error) {
	return s.setEnabled(ctx, name, true, reason)
}

func (s *GatewayRuleService) setEnabled(ctx context.Context, name string, enabled bool, reason string) (*EnableChange, error) {
	var change *EnableChange
	err := s.repo.Tx(ctx, func(txRepo port.GatewayRuleRepository) error {
		rule, err := txRepo.FindByName(ctx, name)
		if err != nil {
			return err
		}
		before := rule.Enabled
		rule.Enabled = enabled
		rule.Version++
		rule.UpdatedAt = time.Now()
		if err := txRepo.Upsert(ctx, rule); err != nil {
			return err
		}
		snapVersion, err := recordSnapshot(ctx, txRepo, reason)
		if err != nil {
			return err
		}
		change = &EnableChange{
			Name:          name,
			BeforeEnabled: before,
			AfterEnabled:  enabled,
			Reason:        reason,
			Version:       snapVersion,
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return change, nil
}

// SetWeights wholesale-replaces the weights of all targets in a rule. The
// request must contain exactly the rule's current target set (matched by
// service+lane): a missing or extra target is rejected. Individual weight 0 is
// allowed (to drain a target), negatives are rejected, and the sum must be 100.
// The new weights are validated through the existing multi-target validator.
func (s *GatewayRuleService) SetWeights(ctx context.Context, name string, req SetWeightsRequest) (*WeightsChange, error) {
	var change *WeightsChange
	err := s.repo.Tx(ctx, func(txRepo port.GatewayRuleRepository) error {
		rule, err := txRepo.FindByName(ctx, name)
		if err != nil {
			return err
		}

		before := make([]domain.GatewayTarget, len(rule.Targets))
		copy(before, rule.Targets)

		if len(req.Weights) != len(rule.Targets) {
			return fmt.Errorf(
				"%w: weights must list exactly the rule's %d target(s), got %d",
				domain.ErrInvalidInput, len(rule.Targets), len(req.Weights),
			)
		}

		// Index incoming weights by service+lane; reject duplicate identities.
		type key struct{ service, lane string }
		incoming := make(map[key]int, len(req.Weights))
		for _, w := range req.Weights {
			k := key{w.Service, w.Lane}
			if _, dup := incoming[k]; dup {
				return fmt.Errorf(
					"%w: duplicate target identity service=%q lane=%q in weights",
					domain.ErrInvalidInput, w.Service, w.Lane,
				)
			}
			incoming[k] = w.Weight
		}

		// Apply by matching each existing target to an incoming weight. A missing
		// match means the request omitted a target; a leftover incoming entry means
		// the request had an extra one.
		after := make([]domain.GatewayTarget, len(rule.Targets))
		copy(after, rule.Targets)
		for i := range after {
			k := key{after[i].Service, after[i].Lane}
			w, ok := incoming[k]
			if !ok {
				return fmt.Errorf(
					"%w: weights missing target service=%q lane=%q present in rule",
					domain.ErrInvalidInput, after[i].Service, after[i].Lane,
				)
			}
			after[i].Weight = w
			delete(incoming, k)
		}
		if len(incoming) > 0 {
			for k := range incoming {
				return fmt.Errorf(
					"%w: weights contains target service=%q lane=%q not present in rule",
					domain.ErrInvalidInput, k.service, k.lane,
				)
			}
		}

		// Reuse the multi-target validator: enforces weight>=0 and sum==100 (plus
		// service/lane/port, which are unchanged here and already valid).
		rule.Targets = after
		if err := domain.ValidateGatewayRule(*rule); err != nil {
			return err
		}

		rule.Version++
		rule.UpdatedAt = time.Now()
		if err := txRepo.Upsert(ctx, rule); err != nil {
			return err
		}
		snapVersion, err := recordSnapshot(ctx, txRepo, req.Reason)
		if err != nil {
			return err
		}

		change = &WeightsChange{
			Name:          name,
			BeforeTargets: before,
			AfterTargets:  after,
			Reason:        req.Reason,
			Version:       snapVersion,
		}
		return nil
	})
	if err != nil {
		return nil, err
	}
	return change, nil
}
