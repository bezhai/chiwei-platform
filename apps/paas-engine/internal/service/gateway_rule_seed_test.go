package service

import (
	"context"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// seedExpectation 是从 routes.yaml 平迁来的 6 条基线规则的预期定义，
// 用来逐条核对 BaselineGatewayRules() 没写错（prefix/service/port/strip_prefix/lane）。
type seedExpectation struct {
	name        string
	pathPrefix  string
	service     string
	port        int
	stripPrefix string
}

func expectedBaselineSeeds() []seedExpectation {
	return []seedExpectation{
		{"default-paas-engine-api", "/api/paas/", "paas-engine", 8080, ""},
		{"default-channel-proxy-lark", "/api/lark/", "channel-proxy", 3003, ""},
		{"default-channel-proxy-webhook", "/webhook/", "channel-proxy", 3003, ""},
		{"default-agent-service-api", "/api/agent/", "agent-service", 8000, "/api/agent"},
		{"default-monitor-dashboard-api", "/dashboard/api/", "monitor-dashboard", 3002, ""},
		{"default-monitor-dashboard-web", "/dashboard/", "monitor-dashboard-web", 80, ""},
	}
}

// TestBaselineGatewayRules_MatchRoutesYaml 逐条核对种子定义跟 routes.yaml 对得上。
func TestBaselineGatewayRules_MatchRoutesYaml(t *testing.T) {
	seeds := BaselineGatewayRules()
	expected := expectedBaselineSeeds()

	if len(seeds) != len(expected) {
		t.Fatalf("expected %d baseline rules, got %d", len(expected), len(seeds))
	}

	byName := make(map[string]BaselineGatewayRule, len(seeds))
	for _, s := range seeds {
		if _, dup := byName[s.Name]; dup {
			t.Fatalf("duplicate seed name %q", s.Name)
		}
		byName[s.Name] = s
	}

	for _, want := range expected {
		got, ok := byName[want.name]
		if !ok {
			t.Errorf("missing baseline rule %q", want.name)
			continue
		}
		req := got.Request

		if got.Name != want.name {
			t.Errorf("%s: name mismatch got %q", want.name, got.Name)
		}
		if req.PathPrefix != want.pathPrefix {
			t.Errorf("%s: path_prefix got %q want %q", want.name, req.PathPrefix, want.pathPrefix)
		}
		if req.Match.PathPrefix != want.pathPrefix {
			t.Errorf("%s: match.path_prefix got %q want %q", want.name, req.Match.PathPrefix, want.pathPrefix)
		}
		if req.Enabled == nil || !*req.Enabled {
			t.Errorf("%s: expected enabled=true", want.name)
		}
		if req.Priority != 100 {
			t.Errorf("%s: priority got %d want 100", want.name, req.Priority)
		}
		if req.RequestLane != "" {
			t.Errorf("%s: request_lane should be empty, got %q", want.name, req.RequestLane)
		}
		if req.Match.RequestLane != "" {
			t.Errorf("%s: match.request_lane should be empty, got %q", want.name, req.Match.RequestLane)
		}
		if len(req.Targets) != 1 {
			t.Fatalf("%s: expected exactly 1 target, got %d", want.name, len(req.Targets))
		}
		tg := req.Targets[0]
		if tg.Service != want.service {
			t.Errorf("%s: target.service got %q want %q", want.name, tg.Service, want.service)
		}
		if tg.Port != want.port {
			t.Errorf("%s: target.port got %d want %d", want.name, tg.Port, want.port)
		}
		// 关键：target.lane 必须留空（透传请求 x-lane），平迁现状。
		if tg.Lane != "" {
			t.Errorf("%s: target.lane MUST be empty (透传), got %q", want.name, tg.Lane)
		}
		if tg.Weight != 100 {
			t.Errorf("%s: target.weight got %d want 100", want.name, tg.Weight)
		}
		if tg.StripPrefix != want.stripPrefix {
			t.Errorf("%s: target.strip_prefix got %q want %q", want.name, tg.StripPrefix, want.stripPrefix)
		}
	}
}

// TestBaselineGatewayRules_PassValidation 确保每条种子都能过 service 层的完整校验。
func TestBaselineGatewayRules_PassValidation(t *testing.T) {
	for _, s := range BaselineGatewayRules() {
		rule := domain.GatewayRule{
			Name:        s.Name,
			Enabled:     s.Request.enabledOrDefault(),
			Priority:    s.Request.Priority,
			PathPrefix:  s.Request.PathPrefix,
			RequestLane: s.Request.RequestLane,
			Match:       s.Request.Match,
			Targets:     s.Request.Targets,
		}
		if err := domain.ValidateGatewayRule(rule); err != nil {
			t.Errorf("baseline rule %q failed validation: %v", s.Name, err)
		}
	}
}

// TestEnsureBaseline_EmptyTableInsertsSix：空表跑一次插入 6 条。
func TestEnsureBaseline_EmptyTableInsertsSix(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)

	if err := svc.EnsureBaseline(context.Background()); err != nil {
		t.Fatal(err)
	}

	rules, err := svc.List(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 6 {
		t.Fatalf("expected 6 rules after ensure on empty table, got %d", len(rules))
	}
	for _, r := range rules {
		if r.Version != 1 {
			t.Errorf("%s: expected fresh insert version 1, got %d", r.Name, r.Version)
		}
	}
}

// TestEnsureBaseline_RerunIdempotent：已有 6 条的表上重跑不报错、不重复、不 bump version。
func TestEnsureBaseline_RerunIdempotent(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)
	ctx := context.Background()

	if err := svc.EnsureBaseline(ctx); err != nil {
		t.Fatal(err)
	}
	if err := svc.EnsureBaseline(ctx); err != nil {
		t.Fatalf("second ensure should be idempotent, got error: %v", err)
	}

	rules, err := svc.List(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(rules) != 6 {
		t.Fatalf("expected still 6 rules after rerun, got %d", len(rules))
	}
	for _, r := range rules {
		// 已存在则不动 -> version 不应被 bump。
		if r.Version != 1 {
			t.Errorf("%s: rerun must not bump version, got %d", r.Name, r.Version)
		}
	}
}

// TestEnsureBaseline_DoesNotOverwriteManualEdit：人工改过的规则不被覆盖。
func TestEnsureBaseline_DoesNotOverwriteManualEdit(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)
	ctx := context.Background()

	// 人工通过管理 API 改了 default-agent-service-api 的 priority。
	manual := validUpsertReq()
	manual.Priority = 500
	manual.PathPrefix = "/api/agent/"
	manual.Match.PathPrefix = "/api/agent/"
	if _, err := svc.Upsert(ctx, "default-agent-service-api", manual); err != nil {
		t.Fatal(err)
	}

	if err := svc.EnsureBaseline(ctx); err != nil {
		t.Fatal(err)
	}

	got, err := svc.Get(ctx, "default-agent-service-api")
	if err != nil {
		t.Fatal(err)
	}
	if got.Priority != 500 {
		t.Errorf("ensure must not overwrite manual edit: priority got %d want 500", got.Priority)
	}

	// 其余 5 条仍被补齐。
	rules, _ := svc.List(ctx)
	if len(rules) != 6 {
		t.Fatalf("expected 6 rules total (1 manual + 5 seeded), got %d", len(rules))
	}
}

// TestEnsureBaseline_UsesInsertIfAbsentNotUpsert：ensure 路径必须走 repo 的
// insert-do-nothing（OnConflict DoNothing），不能走会覆盖的 Upsert——否则存在
// TOCTOU 窗口 + 语义上能冲掉并发插入/人工编辑。
func TestEnsureBaseline_UsesInsertIfAbsentNotUpsert(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)

	if err := svc.EnsureBaseline(context.Background()); err != nil {
		t.Fatal(err)
	}

	if repo.upsertCalls != 0 {
		t.Errorf("ensure must not call Upsert (覆盖语义), got %d calls", repo.upsertCalls)
	}
	if repo.insertIfAbsentCalls != 6 {
		t.Errorf("ensure must call InsertIfAbsent once per seed, got %d", repo.insertIfAbsentCalls)
	}
}

// TestEnsureBaseline_DoesNotOverwriteEvenViaRepoPath：人工改过的规则即使 ensure
// 真的把请求送到 repo，也必须一字不变（do-nothing），证明保护落在 repo 层而非
// 仅靠 service 的 FindByName 预判（后者有 TOCTOU 窗口）。
func TestEnsureBaseline_DoesNotOverwriteEvenViaRepoPath(t *testing.T) {
	repo := newStubGatewayRuleRepo()
	svc := NewGatewayRuleService(repo)
	ctx := context.Background()

	// 人工把 default-monitor-dashboard-web 的 priority 改成 999。
	manual := validUpsertReq()
	manual.Priority = 999
	manual.PathPrefix = "/dashboard/"
	manual.Match.PathPrefix = "/dashboard/"
	manual.Targets[0].Service = "monitor-dashboard-web"
	manual.Targets[0].Port = 80
	if _, err := svc.Upsert(ctx, "default-monitor-dashboard-web", manual); err != nil {
		t.Fatal(err)
	}
	repo.upsertCalls = 0 // 重置，只关心 ensure 是否再次 Upsert

	if err := svc.EnsureBaseline(ctx); err != nil {
		t.Fatal(err)
	}

	got, err := svc.Get(ctx, "default-monitor-dashboard-web")
	if err != nil {
		t.Fatal(err)
	}
	if got.Priority != 999 {
		t.Errorf("ensure overwrote manual edit: priority got %d want 999", got.Priority)
	}
	if got.Targets[0].Service != "monitor-dashboard-web" {
		t.Errorf("ensure overwrote manual edit: service got %q", got.Targets[0].Service)
	}
	if repo.upsertCalls != 0 {
		t.Errorf("ensure must never Upsert(覆盖), got %d calls", repo.upsertCalls)
	}
}
