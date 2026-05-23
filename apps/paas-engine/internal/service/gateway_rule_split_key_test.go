package service

import (
	"context"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

func gwMatch(prefix, lane string) domain.GatewayMatch {
	return domain.GatewayMatch{PathPrefix: prefix, RequestLane: lane}
}

func twoTargets() []domain.GatewayTarget {
	return []domain.GatewayTarget{
		{Service: "agent-a", Lane: "prod", Port: 8000, Weight: 90},
		{Service: "agent-b", Lane: "ppe-new", Port: 8000, Weight: 10},
	}
}

// TestUpsertPersistsSplitKeyHeaders: the upsert request carries
// split_key_headers through to the stored rule.
func TestUpsertPersistsSplitKeyHeaders(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)

	enabled := true
	req := UpsertGatewayRuleRequest{
		Enabled:         &enabled,
		Priority:        100,
		PathPrefix:      "/api/agent/",
		SplitKeyHeaders: []string{"X-User-Id", "X-Trace-Id"},
		Match:           gwMatch("/api/agent/", ""),
		Targets:         twoTargets(),
	}
	if _, err := svc.Upsert(context.Background(), "agent", req); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	stored, err := svc.Get(context.Background(), "agent")
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if len(stored.SplitKeyHeaders) != 2 ||
		stored.SplitKeyHeaders[0] != "X-User-Id" ||
		stored.SplitKeyHeaders[1] != "X-Trace-Id" {
		t.Errorf("split_key_headers not persisted: %+v", stored.SplitKeyHeaders)
	}
}

// TestSnapshotIncludesSplitKeyHeaders: the downstream snapshot carries
// split_key_headers so api-gateway can do stable split.
func TestSnapshotIncludesSplitKeyHeaders(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)

	enabled := true
	req := UpsertGatewayRuleRequest{
		Enabled:         &enabled,
		Priority:        100,
		PathPrefix:      "/api/agent/",
		SplitKeyHeaders: []string{"X-User-Id"},
		Match:           gwMatch("/api/agent/", ""),
		Targets:         twoTargets(),
	}
	if _, err := svc.Upsert(context.Background(), "agent", req); err != nil {
		t.Fatalf("upsert: %v", err)
	}
	snap, err := svc.Snapshot(context.Background())
	if err != nil {
		t.Fatalf("snapshot: %v", err)
	}
	if len(snap.Rules) != 1 {
		t.Fatalf("expected 1 rule, got %d", len(snap.Rules))
	}
	got := snap.Rules[0].SplitKeyHeaders
	if len(got) != 1 || got[0] != "X-User-Id" {
		t.Errorf("snapshot rule split_key_headers: %+v", got)
	}
}

// TestUpsertRejectsInvalidSplitKeyHeader: validation runs in the service path —
// an invalid header name is rejected before persistence.
func TestUpsertRejectsInvalidSplitKeyHeader(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)

	enabled := true
	req := UpsertGatewayRuleRequest{
		Enabled:         &enabled,
		Priority:        100,
		PathPrefix:      "/api/agent/",
		SplitKeyHeaders: []string{"bad header"},
		Match:           gwMatch("/api/agent/", ""),
		Targets:         twoTargets(),
	}
	if _, err := svc.Upsert(context.Background(), "agent", req); err == nil {
		t.Fatal("expected upsert to reject invalid split_key_header name")
	}
}
