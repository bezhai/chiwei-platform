package service

import (
	"context"
	"errors"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// --- stub for GatewayRuleRepository ---

type stubGatewayRuleRepo struct {
	rules               map[string]*domain.GatewayRule
	upsertCalls         int
	insertIfAbsentCalls int

	// 快照历史：snapshotSeq 模拟 PG 序列的独立单调分配（max(已分配)+1）。
	snapshots   []domain.GatewayRuleSnapshot
	snapshotSeq int64
}

func newStubGatewayRuleRepo() *stubGatewayRuleRepo {
	return &stubGatewayRuleRepo{rules: make(map[string]*domain.GatewayRule)}
}

// Tx 内存实现：直接拿自己当事务 repo 跑 fn（无回滚，足够驱动 service 逻辑测试）。
func (r *stubGatewayRuleRepo) Tx(_ context.Context, fn func(repo port.GatewayRuleRepository) error) error {
	return fn(r)
}

// SaveSnapshot 分配下一个独立单调 snapshot_version 并落一条历史。
func (r *stubGatewayRuleRepo) SaveSnapshot(_ context.Context, rules []domain.GatewayRule, createdBy, reason string) (int64, error) {
	r.snapshotSeq++
	cp := make([]domain.GatewayRule, len(rules))
	copy(cp, rules)
	r.snapshots = append(r.snapshots, domain.GatewayRuleSnapshot{
		SnapshotVersion: r.snapshotSeq,
		Rules:           cp,
		CreatedBy:       createdBy,
		Reason:          reason,
	})
	return r.snapshotSeq, nil
}

func (r *stubGatewayRuleRepo) LatestSnapshotVersion(_ context.Context) (int64, error) {
	var maxV int64
	for _, s := range r.snapshots {
		if s.SnapshotVersion > maxV {
			maxV = s.SnapshotVersion
		}
	}
	return maxV, nil
}

func (r *stubGatewayRuleRepo) ListSnapshots(_ context.Context, limit int) ([]*domain.GatewayRuleSnapshot, error) {
	out := make([]*domain.GatewayRuleSnapshot, 0, len(r.snapshots))
	for i := len(r.snapshots) - 1; i >= 0; i-- {
		cp := r.snapshots[i]
		out = append(out, &cp)
		if limit > 0 && len(out) >= limit {
			break
		}
	}
	return out, nil
}

func (r *stubGatewayRuleRepo) GetSnapshot(_ context.Context, version int64) (*domain.GatewayRuleSnapshot, error) {
	for i := range r.snapshots {
		if r.snapshots[i].SnapshotVersion == version {
			cp := r.snapshots[i]
			return &cp, nil
		}
	}
	return nil, domain.ErrGatewayRuleNotFound
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
	}
}

type service_UpsertReq = UpsertGatewayRuleRequest

// --- tests ---

func TestGatewayRuleService_UpsertThenGet(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	if _, err := svc.Upsert(context.Background(), "default-agent-service-api", validUpsertReq()); err != nil {
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
	_, err := svc.Upsert(context.Background(), "bad-rule", req)
	if err == nil {
		t.Fatal("expected validation error")
	}
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput, got %v", err)
	}
}

func TestGatewayRuleService_UpsertRejectsBadName(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	_, err := svc.Upsert(context.Background(), "Bad_Name", validUpsertReq())
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput, got %v", err)
	}
}

func TestGatewayRuleService_UpsertBumpsVersionAndPreservesCreatedAt(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	ctx := context.Background()
	if _, err := svc.Upsert(ctx, "r1", validUpsertReq()); err != nil {
		t.Fatal(err)
	}
	first, _ := svc.Get(ctx, "r1")

	req := validUpsertReq()
	req.Priority = 200
	if _, err := svc.Upsert(ctx, "r1", req); err != nil {
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
	if _, err := svc.Upsert(ctx, "r1", validUpsertReq()); err != nil {
		t.Fatal(err)
	}
	if _, err := svc.Delete(ctx, "r1", "test cleanup"); err != nil {
		t.Fatal(err)
	}
	_, err := svc.Get(ctx, "r1")
	if !errors.Is(err, domain.ErrGatewayRuleNotFound) {
		t.Fatalf("expected not found after delete, got %v", err)
	}
}

func TestGatewayRuleService_DeleteMissing(t *testing.T) {
	svc := NewGatewayRuleService(newStubGatewayRuleRepo())
	_, err := svc.Delete(context.Background(), "nope", "test")
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
	if _, err := svc.Upsert(ctx, "rule-low", low); err != nil {
		t.Fatal(err)
	}

	high := validUpsertReq()
	high.Priority = 200
	high.PathPrefix = "/b/"
	high.Match.PathPrefix = "/b/"
	if _, err := svc.Upsert(ctx, "rule-high", high); err != nil {
		t.Fatal(err)
	}
	// bump rule-high again so its rule version is highest -> snapshot version
	if _, err := svc.Upsert(ctx, "rule-high", high); err != nil {
		t.Fatal(err)
	}

	snap, err := svc.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(snap.Rules) != 2 {
		t.Fatalf("expected 2 rules in snapshot, got %d", len(snap.Rules))
	}
	// snapshot version 来自独立单调序列：3 次 upsert -> 3 条快照 -> version 3
	// （不再是 max(rule.version)=2）。
	if snap.Version != 3 {
		t.Errorf("expected snapshot version 3 (independent sequence over 3 writes), got %d", snap.Version)
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
