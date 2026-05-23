package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// seedRule writes a rule directly into the stub repo for emergency-op tests.
func seedRule(repo *stubGatewayRuleRepo, rule *domain.GatewayRule) {
	cp := *rule
	repo.rules[rule.Name] = &cp
}

func twoTargetRule() *domain.GatewayRule {
	return &domain.GatewayRule{
		Name:        "agent",
		Enabled:     true,
		Priority:    100,
		PathPrefix:  "/api/agent/",
		RequestLane: "",
		Match:       domain.GatewayMatch{PathPrefix: "/api/agent/"},
		Targets: []domain.GatewayTarget{
			{Service: "agent-service", Lane: "prod", Port: 8000, Weight: 90},
			{Service: "agent-service", Lane: "ppe-new", Port: 8000, Weight: 10},
		},
		Version: 3,
	}
}

func TestDisableSetsEnabledFalseAndReportsBeforeAfter(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	seedRule(repo, twoTargetRule())
	svc := NewGatewayRuleService(repo)

	res, err := svc.Disable(context.Background(), "agent", "incident-1234")
	if err != nil {
		t.Fatalf("Disable error: %v", err)
	}
	if res.BeforeEnabled != true || res.AfterEnabled != false {
		t.Errorf("before/after enabled = %v/%v, want true/false", res.BeforeEnabled, res.AfterEnabled)
	}
	if res.Reason != "incident-1234" {
		t.Errorf("reason=%q want incident-1234", res.Reason)
	}
	got, _ := repo.FindByName(context.Background(), "agent")
	if got.Enabled {
		t.Error("rule still enabled after Disable")
	}
	if got.Version != 4 {
		t.Errorf("version=%d want 4 (bumped)", got.Version)
	}
}

func TestEnableSetsEnabledTrue(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	r := twoTargetRule()
	r.Enabled = false
	seedRule(repo, r)
	svc := NewGatewayRuleService(repo)

	res, err := svc.Enable(context.Background(), "agent", "recovered")
	if err != nil {
		t.Fatalf("Enable error: %v", err)
	}
	if res.BeforeEnabled != false || res.AfterEnabled != true {
		t.Errorf("before/after = %v/%v want false/true", res.BeforeEnabled, res.AfterEnabled)
	}
	got, _ := repo.FindByName(context.Background(), "agent")
	if !got.Enabled {
		t.Error("rule not enabled after Enable")
	}
}

func TestDisableMissingRuleReturnsNotFound(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	_, err := svc.Disable(context.Background(), "nope", "x")
	if !errors.Is(err, domain.ErrGatewayRuleNotFound) {
		t.Fatalf("expected ErrGatewayRuleNotFound, got %v", err)
	}
}

func TestSetWeightsDrainsTargetToZero(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	seedRule(repo, twoTargetRule())
	svc := NewGatewayRuleService(repo)

	req := SetWeightsRequest{
		Reason: "drain ppe-new",
		Weights: []TargetWeight{
			{Service: "agent-service", Lane: "prod", Weight: 100},
			{Service: "agent-service", Lane: "ppe-new", Weight: 0},
		},
	}
	res, err := svc.SetWeights(context.Background(), "agent", req)
	if err != nil {
		t.Fatalf("SetWeights error: %v", err)
	}
	// before: 90/10, after: 100/0
	if findWeight(res.BeforeTargets, "agent-service", "prod") != 90 ||
		findWeight(res.BeforeTargets, "agent-service", "ppe-new") != 10 {
		t.Errorf("before weights wrong: %+v", res.BeforeTargets)
	}
	if findWeight(res.AfterTargets, "agent-service", "prod") != 100 ||
		findWeight(res.AfterTargets, "agent-service", "ppe-new") != 0 {
		t.Errorf("after weights wrong: %+v", res.AfterTargets)
	}
	got, _ := repo.FindByName(context.Background(), "agent")
	for _, tg := range got.Targets {
		if tg.Lane == "ppe-new" && tg.Weight != 0 {
			t.Errorf("ppe-new weight=%d want 0", tg.Weight)
		}
		if tg.Lane == "prod" && tg.Weight != 100 {
			t.Errorf("prod weight=%d want 100", tg.Weight)
		}
	}
}

func TestSetWeightsRejectsSumNot100(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	seedRule(repo, twoTargetRule())
	svc := NewGatewayRuleService(repo)

	req := SetWeightsRequest{
		Reason: "bad",
		Weights: []TargetWeight{
			{Service: "agent-service", Lane: "prod", Weight: 50},
			{Service: "agent-service", Lane: "ppe-new", Weight: 40},
		},
	}
	_, err := svc.SetWeights(context.Background(), "agent", req)
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput for sum!=100, got %v", err)
	}
}

func TestSetWeightsRejectsNegative(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	seedRule(repo, twoTargetRule())
	svc := NewGatewayRuleService(repo)

	req := SetWeightsRequest{
		Reason: "bad",
		Weights: []TargetWeight{
			{Service: "agent-service", Lane: "prod", Weight: 110},
			{Service: "agent-service", Lane: "ppe-new", Weight: -10},
		},
	}
	_, err := svc.SetWeights(context.Background(), "agent", req)
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput for negative weight, got %v", err)
	}
}

func TestSetWeightsRejectsMissingTarget(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	seedRule(repo, twoTargetRule())
	svc := NewGatewayRuleService(repo)

	// only one target supplied, rule has two -> missing target rejected
	req := SetWeightsRequest{
		Reason: "bad",
		Weights: []TargetWeight{
			{Service: "agent-service", Lane: "prod", Weight: 100},
		},
	}
	_, err := svc.SetWeights(context.Background(), "agent", req)
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput for missing target, got %v", err)
	}
}

func TestSetWeightsRejectsExtraTarget(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	seedRule(repo, twoTargetRule())
	svc := NewGatewayRuleService(repo)

	// an extra target not present in the rule -> rejected
	req := SetWeightsRequest{
		Reason: "bad",
		Weights: []TargetWeight{
			{Service: "agent-service", Lane: "prod", Weight: 50},
			{Service: "agent-service", Lane: "ppe-new", Weight: 30},
			{Service: "agent-service", Lane: "ppe-other", Weight: 20},
		},
	}
	_, err := svc.SetWeights(context.Background(), "agent", req)
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput for extra target, got %v", err)
	}
}

func TestSetWeightsMissingRuleReturnsNotFound(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	_, err := svc.SetWeights(context.Background(), "nope", SetWeightsRequest{
		Weights: []TargetWeight{{Service: "x", Lane: "prod", Weight: 100}},
	})
	if !errors.Is(err, domain.ErrGatewayRuleNotFound) {
		t.Fatalf("expected ErrGatewayRuleNotFound, got %v", err)
	}
}

func findWeight(targets []domain.GatewayTarget, service, lane string) int {
	for _, t := range targets {
		if t.Service == service && t.Lane == lane {
			return t.Weight
		}
	}
	return -999
}
