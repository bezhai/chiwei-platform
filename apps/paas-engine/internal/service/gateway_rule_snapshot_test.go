package service

import (
	"context"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

func ruleReq(prefix string) UpsertGatewayRuleRequest {
	enabled := true
	return UpsertGatewayRuleRequest{
		Enabled:    &enabled,
		Priority:   100,
		PathPrefix: prefix,
		Match:      domain.GatewayMatch{PathPrefix: prefix},
		Targets: []domain.GatewayTarget{
			{Service: "agent-service", Lane: "prod", Port: 8000, Weight: 100},
		},
	}
}

// 核心断言①：连续若干次写操作（含一次 delete）后能列出对应数量历史快照、版本严格单调递增。
func TestGatewaySnapshot_HistoryMonotonicAcrossWrites(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)
	ctx := context.Background()

	mustUpsert(t, svc, "ra", ruleReq("/a/"))         // write 1
	mustUpsert(t, svc, "rb", ruleReq("/b/"))         // write 2
	mustUpsert(t, svc, "ra", ruleReq("/a/"))         // write 3 (update)
	if _, err := svc.Disable(ctx, "rb", "stop"); err != nil {   // write 4
		t.Fatal(err)
	}
	mustDelete(t, svc, "ra", "cleanup") // write 5 (含 delete)

	snaps, err := svc.ListSnapshots(ctx, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(snaps) != 5 {
		t.Fatalf("expected 5 history snapshots after 5 writes, got %d", len(snaps))
	}
	// newest first -> versions strictly decreasing as we go; collect and assert strict-monotone overall.
	seen := make([]int64, 0, len(snaps))
	for _, s := range snaps {
		seen = append(seen, s.SnapshotVersion)
	}
	// snaps come newest-first; reverse to chronological and assert strictly increasing.
	for i := 0; i < len(seen); i++ {
		want := int64(len(seen) - i)
		if seen[i] != want {
			t.Fatalf("snapshot versions not strictly monotone (newest-first): got %v", seen)
		}
	}
}

// 核心断言②：删掉当前 version 最高的规则后，snapshot version 不回退（切独立序列的核心动机）。
func TestGatewaySnapshot_DeleteHighestVersionRuleDoesNotRegress(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)
	ctx := context.Background()

	// rb 被反复 upsert，rule.Version 升到最高（旧逻辑 max(rule.version) 会盯它）。
	mustUpsert(t, svc, "ra", ruleReq("/a/"))
	mustUpsert(t, svc, "rb", ruleReq("/b/"))
	mustUpsert(t, svc, "rb", ruleReq("/b/"))
	mustUpsert(t, svc, "rb", ruleReq("/b/")) // rb.Version == 3, 全场最高

	beforeSnap, err := svc.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	// 删掉 version 最高的 rb：旧逻辑 max(rule.version) 会从 3 回退到 ra 的 1。
	mustDelete(t, svc, "rb", "drop highest")

	afterSnap, err := svc.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if afterSnap.Version <= beforeSnap.Version {
		t.Fatalf("snapshot version regressed after deleting highest-rule-version: before=%d after=%d",
			beforeSnap.Version, afterSnap.Version)
	}
}

// 核心断言③：回滚到上一版后规则集恢复，且 snapshot version 是更大的新值（非倒退）。
func TestGatewaySnapshot_RollbackRestoresRulesWithNewerVersion(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)
	ctx := context.Background()

	mustUpsert(t, svc, "ra", ruleReq("/a/"))
	mustUpsert(t, svc, "rb", ruleReq("/b/")) // snapshot v2: {ra, rb}

	snaps, err := svc.ListSnapshots(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	targetVersion := snaps[0].SnapshotVersion // 最新 = v2，含 {ra, rb}

	// 把 rb 删掉，当前只剩 ra（snapshot v3）。
	mustDelete(t, svc, "rb", "oops")
	mid, _ := svc.List(ctx)
	if len(mid) != 1 {
		t.Fatalf("expected 1 rule after delete, got %d", len(mid))
	}

	// 回滚到 v2（含 {ra, rb}），应生成 snapshot v4。
	rb, err := svc.Rollback(ctx, targetVersion, "rollback to v2")
	if err != nil {
		t.Fatal(err)
	}
	if rb.SnapshotVersion <= targetVersion {
		t.Fatalf("rollback must assign a NEWER snapshot version, target=%d got=%d",
			targetVersion, rb.SnapshotVersion)
	}

	restored, _ := svc.List(ctx)
	if len(restored) != 2 {
		t.Fatalf("expected ruleset restored to 2 rules after rollback, got %d", len(restored))
	}
	cur, err := svc.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if cur.Version != rb.SnapshotVersion {
		t.Fatalf("current snapshot version should equal rollback's new version: %d vs %d",
			cur.Version, rb.SnapshotVersion)
	}
}

// 核心断言④：事务边界——连续两次写不产生同版，且历史与当前一致（最新快照规则集 == 当前规则集）。
func TestGatewaySnapshot_TxBoundaryNoDupVersionAndHistoryMatchesCurrent(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)
	ctx := context.Background()

	mustUpsert(t, svc, "ra", ruleReq("/a/"))
	mustUpsert(t, svc, "rb", ruleReq("/b/"))

	snaps, err := svc.ListSnapshots(ctx, 10)
	if err != nil {
		t.Fatal(err)
	}
	versions := map[int64]bool{}
	for _, s := range snaps {
		if versions[s.SnapshotVersion] {
			t.Fatalf("duplicate snapshot version produced: %d", s.SnapshotVersion)
		}
		versions[s.SnapshotVersion] = true
	}

	// 最新快照的规则集必须和当前 List 一致（历史与当前不脱节）。
	latest := snaps[0]
	current, _ := svc.List(ctx)
	if len(latest.Rules) != len(current) {
		t.Fatalf("latest snapshot ruleset size %d != current %d", len(latest.Rules), len(current))
	}
	names := map[string]bool{}
	for _, r := range latest.Rules {
		names[r.Name] = true
	}
	for _, r := range current {
		if !names[r.Name] {
			t.Fatalf("rule %q present currently but missing from latest snapshot", r.Name)
		}
	}
}

// mustUpsert / mustDelete 包掉 (snapshot_version, error) 返回，断言无错并回传版本号。
func mustUpsert(t *testing.T, svc *GatewayRuleService, name string, req UpsertGatewayRuleRequest) int64 {
	t.Helper()
	v, err := svc.Upsert(context.Background(), name, req)
	if err != nil {
		t.Fatal(err)
	}
	return v
}

func mustDelete(t *testing.T, svc *GatewayRuleService, name, reason string) int64 {
	t.Helper()
	v, err := svc.Delete(context.Background(), name, reason)
	if err != nil {
		t.Fatal(err)
	}
	return v
}
