package repository

import (
	"encoding/json"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

func sampleGatewayRule() *domain.GatewayRule {
	return &domain.GatewayRule{
		Name:        "default-agent-service-api",
		Enabled:     true,
		Priority:    100,
		PathPrefix:  "/api/agent/",
		RequestLane: "",
		Match: domain.GatewayMatch{
			PathPrefix: "/api/agent/",
		},
		Targets: []domain.GatewayTarget{
			{Service: "agent-service", Lane: "prod", Port: 8000, Weight: 100, StripPrefix: "/api/agent"},
		},
		CreatedAt: time.Now(),
		UpdatedAt: time.Now(),
		Version:   3,
	}
}

func TestGatewayRuleToModel_SerializesJSON(t *testing.T) {
	rule := sampleGatewayRule()
	m, err := gatewayRuleToModel(rule)
	if err != nil {
		t.Fatalf("gatewayRuleToModel failed: %v", err)
	}
	if m.Name != "default-agent-service-api" {
		t.Errorf("name mismatch: %q", m.Name)
	}
	if m.PathPrefix != "/api/agent/" {
		t.Errorf("path_prefix top-level column mismatch: %q", m.PathPrefix)
	}
	if m.Version != 3 {
		t.Errorf("version mismatch: %d", m.Version)
	}

	var match domain.GatewayMatch
	if err := json.Unmarshal([]byte(m.Match), &match); err != nil {
		t.Fatalf("match not valid JSON: %v", err)
	}
	if match.PathPrefix != "/api/agent/" {
		t.Errorf("match.path_prefix mismatch: %q", match.PathPrefix)
	}

	var targets []domain.GatewayTarget
	if err := json.Unmarshal([]byte(m.Targets), &targets); err != nil {
		t.Fatalf("targets not valid JSON: %v", err)
	}
	if len(targets) != 1 || targets[0].StripPrefix != "/api/agent" {
		t.Errorf("targets not preserved: %+v", targets)
	}
}

func TestGatewayRuleRoundTrip(t *testing.T) {
	original := sampleGatewayRule()
	m, err := gatewayRuleToModel(original)
	if err != nil {
		t.Fatalf("gatewayRuleToModel failed: %v", err)
	}
	got, err := modelToGatewayRule(m)
	if err != nil {
		t.Fatalf("modelToGatewayRule failed: %v", err)
	}

	if got.Name != original.Name {
		t.Errorf("name mismatch: %q vs %q", got.Name, original.Name)
	}
	if got.Enabled != original.Enabled {
		t.Errorf("enabled mismatch")
	}
	if got.Priority != original.Priority {
		t.Errorf("priority mismatch: %d", got.Priority)
	}
	if got.PathPrefix != original.PathPrefix {
		t.Errorf("path_prefix mismatch: %q", got.PathPrefix)
	}
	if got.Version != original.Version {
		t.Errorf("version mismatch: %d", got.Version)
	}
	if got.Match.PathPrefix != original.Match.PathPrefix {
		t.Errorf("match.path_prefix mismatch: %q", got.Match.PathPrefix)
	}
	if len(got.Targets) != 1 {
		t.Fatalf("targets length mismatch: %d", len(got.Targets))
	}
	if got.Targets[0].Service != "agent-service" || got.Targets[0].Port != 8000 {
		t.Errorf("target mismatch: %+v", got.Targets[0])
	}
	if got.Targets[0].StripPrefix != "/api/agent" {
		t.Errorf("strip_prefix not preserved: %q", got.Targets[0].StripPrefix)
	}
}

func TestModelToGatewayRule_TargetsNeverNil(t *testing.T) {
	m := &GatewayRuleModel{
		Name:       "x",
		PathPrefix: "/x/",
		Match:      `{"path_prefix":"/x/"}`,
		Targets:    "",
	}
	got, err := modelToGatewayRule(m)
	if err != nil {
		t.Fatalf("modelToGatewayRule failed: %v", err)
	}
	if got.Targets == nil {
		t.Error("expected non-nil empty targets slice")
	}
}

func TestModelToGatewaySnapshot_RoundTrip(t *testing.T) {
	rules := []domain.GatewayRule{
		{Name: "ra", PathPrefix: "/a/", Match: domain.GatewayMatch{PathPrefix: "/a/"}, Version: 2},
		{Name: "rb", PathPrefix: "/b/", Match: domain.GatewayMatch{PathPrefix: "/b/"}, Version: 1},
	}
	rulesJSON, err := json.Marshal(rules)
	if err != nil {
		t.Fatalf("marshal rules: %v", err)
	}
	m := &GatewayRuleSnapshotModel{
		SnapshotVersion: 7,
		Rules:           string(rulesJSON),
		CreatedBy:       "ops",
		Reason:          "rollback to v2",
		CreatedAt:       time.Now(),
	}
	got, err := modelToGatewaySnapshot(m)
	if err != nil {
		t.Fatalf("modelToGatewaySnapshot failed: %v", err)
	}
	if got.SnapshotVersion != 7 {
		t.Errorf("snapshot_version mismatch: %d", got.SnapshotVersion)
	}
	if got.CreatedBy != "ops" || got.Reason != "rollback to v2" {
		t.Errorf("created_by/reason not preserved: %q / %q", got.CreatedBy, got.Reason)
	}
	if len(got.Rules) != 2 || got.Rules[0].Name != "ra" || got.Rules[1].Name != "rb" {
		t.Errorf("rules not round-tripped: %+v", got.Rules)
	}
}

func TestModelToGatewaySnapshot_EmptyRulesNeverNil(t *testing.T) {
	got, err := modelToGatewaySnapshot(&GatewayRuleSnapshotModel{SnapshotVersion: 1, Rules: ""})
	if err != nil {
		t.Fatalf("modelToGatewaySnapshot failed: %v", err)
	}
	if got.Rules == nil {
		t.Error("expected non-nil empty rules slice")
	}
}
