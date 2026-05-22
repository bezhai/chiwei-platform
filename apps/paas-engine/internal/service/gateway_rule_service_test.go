package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stub for GatewayRuleRepository ---

type stubGatewayRuleRepo struct {
	rules               map[string]*domain.GatewayRule
	upsertCalls         int
	insertIfAbsentCalls int
}

func newStubGatewayRuleRepo() *stubGatewayRuleRepo {
	return &stubGatewayRuleRepo{rules: make(map[string]*domain.GatewayRule)}
}

func (r *stubGatewayRuleRepo) Upsert(_ context.Context, rule *domain.GatewayRule) error {
	r.upsertCalls++
	cp := *rule
	r.rules[rule.Name] = &cp
	return nil
}

// InsertIfAbsent 模拟 OnConflict DoNothing：已存在则保持原值不动、返回成功。
func (r *stubGatewayRuleRepo) InsertIfAbsent(_ context.Context, rule *domain.GatewayRule) error {
	r.insertIfAbsentCalls++
	if _, exists := r.rules[rule.Name]; exists {
		return nil // 冲突不更新
	}
	cp := *rule
	r.rules[rule.Name] = &cp
	return nil
}

func (r *stubGatewayRuleRepo) FindByName(_ context.Context, name string) (*domain.GatewayRule, error) {
	rule, ok := r.rules[name]
	if !ok {
		return nil, domain.ErrGatewayRuleNotFound
	}
	cp := *rule
	return &cp, nil
}

func (r *stubGatewayRuleRepo) FindAll(_ context.Context) ([]*domain.GatewayRule, error) {
	out := make([]*domain.GatewayRule, 0, len(r.rules))
	for _, rule := range r.rules {
		cp := *rule
		out = append(out, &cp)
	}
	return out, nil
}

func (r *stubGatewayRuleRepo) Delete(_ context.Context, name string) error {
	if _, ok := r.rules[name]; !ok {
		return domain.ErrGatewayRuleNotFound
	}
	delete(r.rules, name)
	return nil
}

// --- helpers ---

func validUpsertReq() service_UpsertReq {
	enabled := true
	return service_UpsertReq{
		Enabled:    &enabled,
		Priority:   100,
		PathPrefix: "/api/agent/",
		Match: domain.GatewayMatch{
			PathPrefix: "/api/agent/",
		},
		Targets: []domain.GatewayTarget{
			{Service: "agent-service", Lane: "prod", Port: 8000, Weight: 100},
		},
		Fallback: domain.GatewayFallback{Mode: "prod"},
	}
}

type service_UpsertReq = UpsertGatewayRuleRequest

// --- tests ---

func TestGatewayRuleService_UpsertThenGet(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	if err := svc.Upsert(context.Background(), "default-agent-service-api", validUpsertReq()); err != nil {
		t.Fatal(err)
	}
	rule, err := svc.Get(context.Background(), "default-agent-service-api")
	if err != nil {
		t.Fatal(err)
	}
	if rule.Name != "default-agent-service-api" {
		t.Errorf("expected name to be set from path key, got %q", rule.Name)
	}
	if rule.Version != 1 {
		t.Errorf("expected initial version 1, got %d", rule.Version)
	}
	if rule.CreatedAt.IsZero() || rule.UpdatedAt.IsZero() {
		t.Error("expected created_at/updated_at to be set")
	}
}

func TestGatewayRuleService_UpsertRejectsInvalid(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	req := validUpsertReq()
	req.PathPrefix = "no-slash/"
	req.Match.PathPrefix = "no-slash/"
	err := svc.Upsert(context.Background(), "bad-rule", req)
	if err == nil {
		t.Fatal("expected validation error")
	}
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput, got %v", err)
	}
}

func TestGatewayRuleService_UpsertRejectsBadName(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	err := svc.Upsert(context.Background(), "Bad_Name", validUpsertReq())
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput, got %v", err)
	}
}

func TestGatewayRuleService_UpsertBumpsVersionAndPreservesCreatedAt(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	ctx := context.Background()
	if err := svc.Upsert(ctx, "r1", validUpsertReq()); err != nil {
		t.Fatal(err)
	}
	first, _ := svc.Get(ctx, "r1")

	req := validUpsertReq()
	req.Priority = 200
	if err := svc.Upsert(ctx, "r1", req); err != nil {
		t.Fatal(err)
	}
	second, _ := svc.Get(ctx, "r1")

	if second.Version != 2 {
		t.Errorf("expected version bumped to 2, got %d", second.Version)
	}
	if second.Priority != 200 {
		t.Errorf("expected priority 200, got %d", second.Priority)
	}
	if !second.CreatedAt.Equal(first.CreatedAt) {
		t.Errorf("expected created_at preserved across upsert: %v vs %v", first.CreatedAt, second.CreatedAt)
	}
}

func TestGatewayRuleService_ListEmpty(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	rules, err := svc.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if rules == nil {
		t.Error("expected non-nil empty slice")
	}
	if len(rules) != 0 {
		t.Errorf("expected 0 rules, got %d", len(rules))
	}
}

func TestGatewayRuleService_Delete(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	ctx := context.Background()
	if err := svc.Upsert(ctx, "r1", validUpsertReq()); err != nil {
		t.Fatal(err)
	}
	if err := svc.Delete(ctx, "r1"); err != nil {
		t.Fatal(err)
	}
	_, err := svc.Get(ctx, "r1")
	if !errors.Is(err, domain.ErrGatewayRuleNotFound) {
		t.Fatalf("expected not found after delete, got %v", err)
	}
}

func TestGatewayRuleService_DeleteMissing(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	err := svc.Delete(context.Background(), "nope")
	if !errors.Is(err, domain.ErrGatewayRuleNotFound) {
		t.Fatalf("expected not found, got %v", err)
	}
}

func TestGatewayRuleService_SnapshotVersionAndSort(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	ctx := context.Background()

	low := validUpsertReq()
	low.Priority = 50
	low.PathPrefix = "/a/"
	low.Match.PathPrefix = "/a/"
	if err := svc.Upsert(ctx, "rule-low", low); err != nil {
		t.Fatal(err)
	}

	high := validUpsertReq()
	high.Priority = 200
	high.PathPrefix = "/b/"
	high.Match.PathPrefix = "/b/"
	if err := svc.Upsert(ctx, "rule-high", high); err != nil {
		t.Fatal(err)
	}
	// bump rule-high again so its rule version is highest -> snapshot version
	if err := svc.Upsert(ctx, "rule-high", high); err != nil {
		t.Fatal(err)
	}

	snap, err := svc.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Rules) != 2 {
		t.Fatalf("expected 2 rules in snapshot, got %d", len(snap.Rules))
	}
	// snapshot version = max(rule.version) = 2 (rule-high upserted twice)
	if snap.Version != 2 {
		t.Errorf("expected snapshot version 2 (max rule version), got %d", snap.Version)
	}
	// sorted by priority desc -> rule-high (200) first
	if snap.Rules[0].Name != "rule-high" {
		t.Errorf("expected rule-high first (priority desc), got %q", snap.Rules[0].Name)
	}
	if snap.UpdatedAt.IsZero() {
		t.Error("expected snapshot updated_at to be set")
	}
}

func TestGatewayRuleService_SnapshotEmpty(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	snap, err := svc.Snapshot(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if snap.Rules == nil {
		t.Error("expected non-nil empty rules slice")
	}
	if len(snap.Rules) != 0 {
		t.Errorf("expected 0 rules, got %d", len(snap.Rules))
	}
	if snap.Version != 0 {
		t.Errorf("expected snapshot version 0 for empty, got %d", snap.Version)
	}
}
