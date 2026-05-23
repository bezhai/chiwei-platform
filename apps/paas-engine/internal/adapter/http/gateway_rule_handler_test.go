package http

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

// in-memory stub repo for handler tests
type gwStubRepo struct {
	rules     map[string]*domain.GatewayRule
	snapshots []domain.GatewayRuleSnapshot
	seq       int64
}

func newGwStubRepo() *gwStubRepo {
	return &gwStubRepo{rules: make(map[string]*domain.GatewayRule)}
}

func (r *gwStubRepo) Tx(_ context.Context, fn func(repo port.GatewayRuleRepository) error) error {
	return fn(r)
}
func (r *gwStubRepo) SaveSnapshot(_ context.Context, rules []domain.GatewayRule, createdBy, reason string) (int64, error) {
	r.seq++
	cp := make([]domain.GatewayRule, len(rules))
	copy(cp, rules)
	r.snapshots = append(r.snapshots, domain.GatewayRuleSnapshot{
		SnapshotVersion: r.seq, Rules: cp, CreatedBy: createdBy, Reason: reason,
	})
	return r.seq, nil
}
func (r *gwStubRepo) LatestSnapshotVersion(_ context.Context) (int64, error) {
	return r.seq, nil
}
func (r *gwStubRepo) ListSnapshots(_ context.Context, limit int) ([]*domain.GatewayRuleSnapshot, error) {
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
func (r *gwStubRepo) GetSnapshot(_ context.Context, version int64) (*domain.GatewayRuleSnapshot, error) {
	for i := range r.snapshots {
		if r.snapshots[i].SnapshotVersion == version {
			cp := r.snapshots[i]
			return &cp, nil
		}
	}
	return nil, domain.ErrGatewayRuleNotFound
}

func (r *gwStubRepo) Upsert(_ context.Context, rule *domain.GatewayRule) error {
	cp := *rule
	r.rules[rule.Name] = &cp
	return nil
}
func (r *gwStubRepo) InsertIfAbsent(_ context.Context, rule *domain.GatewayRule) error {
	if _, ok := r.rules[rule.Name]; ok {
		return nil
	}
	cp := *rule
	r.rules[rule.Name] = &cp
	return nil
}
func (r *gwStubRepo) FindByName(_ context.Context, name string) (*domain.GatewayRule, error) {
	rule, ok := r.rules[name]
	if !ok {
		return nil, domain.ErrGatewayRuleNotFound
	}
	cp := *rule
	return &cp, nil
}
func (r *gwStubRepo) FindAll(_ context.Context) ([]*domain.GatewayRule, error) {
	out := make([]*domain.GatewayRule, 0, len(r.rules))
	for _, rule := range r.rules {
		cp := *rule
		out = append(out, &cp)
	}
	return out, nil
}
func (r *gwStubRepo) Delete(_ context.Context, name string) error {
	if _, ok := r.rules[name]; !ok {
		return domain.ErrGatewayRuleNotFound
	}
	delete(r.rules, name)
	return nil
}

func newGatewayTestRouter() (*chi.Mux, *GatewayRuleHandler) {
	svc := service.NewGatewayRuleService(newGwStubRepo())
	h := NewGatewayRuleHandler(svc)
	r := chi.NewRouter()
	r.Route("/api/paas/gateway-rules", func(r chi.Router) {
		r.Get("/", h.List)
		r.Get("/{name}", h.Get)
		r.Put("/{name}", h.Upsert)
		r.Delete("/{name}", h.Delete)
	})
	r.Get("/internal/gateway-rules", h.Snapshot)
	return r, h
}

func validRuleBody() string {
	return `{
		"enabled": true,
		"priority": 100,
		"path_prefix": "/api/agent/",
		"match": {"path_prefix": "/api/agent/"},
		"targets": [{"service": "agent-service", "lane": "prod", "port": 8000, "weight": 100, "strip_prefix": "/api/agent"}]
	}`
}

func doReq(t *testing.T, r http.Handler, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	var rdr *bytes.Reader
	if body != "" {
		rdr = bytes.NewReader([]byte(body))
	} else {
		rdr = bytes.NewReader(nil)
	}
	req := httptest.NewRequest(method, path, rdr)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)
	return rec
}

func TestGatewayHandler_PutGetListDelete(t *testing.T) {
	r, _ := newGatewayTestRouter()

	// PUT
	rec := doReq(t, r, http.MethodPut, "/api/paas/gateway-rules/default-agent-service-api", validRuleBody())
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT expected 200, got %d: %s", rec.Code, rec.Body.String())
	}

	// GET single
	rec = doReq(t, r, http.MethodGet, "/api/paas/gateway-rules/default-agent-service-api", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET single expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var env struct {
		Data domain.GatewayRule `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode GET single: %v", err)
	}
	if env.Data.Name != "default-agent-service-api" {
		t.Errorf("expected name from path, got %q", env.Data.Name)
	}
	if env.Data.Version != 1 {
		t.Errorf("expected version 1, got %d", env.Data.Version)
	}
	if env.Data.Targets[0].StripPrefix != "/api/agent" {
		t.Errorf("strip_prefix not round-tripped: %q", env.Data.Targets[0].StripPrefix)
	}

	// GET list
	rec = doReq(t, r, http.MethodGet, "/api/paas/gateway-rules", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET list expected 200, got %d", rec.Code)
	}
	var listEnv struct {
		Data []domain.GatewayRule `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &listEnv); err != nil {
		t.Fatalf("decode list: %v", err)
	}
	if len(listEnv.Data) != 1 {
		t.Fatalf("expected 1 rule in list, got %d", len(listEnv.Data))
	}

	// DELETE
	rec = doReq(t, r, http.MethodDelete, "/api/paas/gateway-rules/default-agent-service-api", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("DELETE expected 200, got %d", rec.Code)
	}

	// GET single after delete -> 404
	rec = doReq(t, r, http.MethodGet, "/api/paas/gateway-rules/default-agent-service-api", "")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("GET after delete expected 404, got %d", rec.Code)
	}
}

// bodyWithoutEnabled / bodyEnabledFalse 复用 validRuleBody 的形状，
// 但分别省略 enabled 和显式 enabled:false，验证 #6 默认启用语义。
func bodyWithoutEnabled() string {
	return `{
		"priority": 100,
		"path_prefix": "/api/agent/",
		"match": {"path_prefix": "/api/agent/"},
		"targets": [{"service": "agent-service", "lane": "prod", "port": 8000, "weight": 100}]
	}`
}

func bodyEnabledFalse() string {
	return `{
		"enabled": false,
		"priority": 100,
		"path_prefix": "/api/agent/",
		"match": {"path_prefix": "/api/agent/"},
		"targets": [{"service": "agent-service", "lane": "prod", "port": 8000, "weight": 100}]
	}`
}

func putAndGetEnabled(t *testing.T, body string) bool {
	t.Helper()
	r, _ := newGatewayTestRouter()
	rec := doReq(t, r, http.MethodPut, "/api/paas/gateway-rules/default-agent-service-api", body)
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	rec = doReq(t, r, http.MethodGet, "/api/paas/gateway-rules/default-agent-service-api", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("GET expected 200, got %d", rec.Code)
	}
	var env struct {
		Data domain.GatewayRule `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode GET: %v", err)
	}
	return env.Data.Enabled
}

func TestGatewayHandler_PutOmittedEnabledDefaultsTrue(t *testing.T) {
	if got := putAndGetEnabled(t, bodyWithoutEnabled()); !got {
		t.Errorf("PUT omitting enabled must default to true, got enabled=%v", got)
	}
}

func TestGatewayHandler_PutExplicitEnabledFalseStaysFalse(t *testing.T) {
	if got := putAndGetEnabled(t, bodyEnabledFalse()); got {
		t.Errorf("PUT with explicit enabled:false must stay false, got enabled=%v", got)
	}
}

func TestGatewayHandler_PutExplicitEnabledTrueStaysTrue(t *testing.T) {
	if got := putAndGetEnabled(t, validRuleBody()); !got {
		t.Errorf("PUT with explicit enabled:true must stay true, got enabled=%v", got)
	}
}

func TestGatewayHandler_PutInvalidReturns400(t *testing.T) {
	r, _ := newGatewayTestRouter()
	body := `{"enabled":true,"priority":100,"path_prefix":"no-slash/","match":{"path_prefix":"no-slash/"},"targets":[{"service":"x","lane":"prod","port":80,"weight":100}]}`
	rec := doReq(t, r, http.MethodPut, "/api/paas/gateway-rules/bad", body)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for invalid rule, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestGatewayHandler_PutMalformedJSON(t *testing.T) {
	r, _ := newGatewayTestRouter()
	rec := doReq(t, r, http.MethodPut, "/api/paas/gateway-rules/bad", "{not json")
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for malformed JSON, got %d", rec.Code)
	}
}

func TestGatewayHandler_DeleteMissing404(t *testing.T) {
	r, _ := newGatewayTestRouter()
	rec := doReq(t, r, http.MethodDelete, "/api/paas/gateway-rules/nope", "")
	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
}

func TestGatewayHandler_SnapshotPayloadShape(t *testing.T) {
	r, _ := newGatewayTestRouter()
	if rec := doReq(t, r, http.MethodPut, "/api/paas/gateway-rules/default-agent-service-api", validRuleBody()); rec.Code != http.StatusOK {
		t.Fatalf("seed PUT failed: %d %s", rec.Code, rec.Body.String())
	}

	rec := doReq(t, r, http.MethodGet, "/internal/gateway-rules", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("snapshot expected 200, got %d", rec.Code)
	}

	// The internal snapshot must NOT be wrapped in {data:...} envelope —
	// it returns the snapshot object directly with version/updated_at/rules.
	var snap struct {
		Version   int64                `json:"version"`
		UpdatedAt string               `json:"updated_at"`
		Rules     []domain.GatewayRule `json:"rules"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &snap); err != nil {
		t.Fatalf("decode snapshot: %v; body=%s", err, rec.Body.String())
	}
	if snap.Version != 1 {
		t.Errorf("expected snapshot version 1, got %d", snap.Version)
	}
	if snap.UpdatedAt == "" {
		t.Error("expected updated_at present")
	}
	if len(snap.Rules) != 1 {
		t.Fatalf("expected 1 rule in snapshot, got %d", len(snap.Rules))
	}
	if snap.Rules[0].Name != "default-agent-service-api" {
		t.Errorf("rule name mismatch: %q", snap.Rules[0].Name)
	}
}

// PUT/DELETE 必须在响应里带回事务分配的 snapshot_version，否则 Dashboard 审计
// 拿不到改后快照版本（只能误取 rule.version 或记 null）。snapshot_version 与
// rule.version 是两套版本号：前者是审计/回滚游标，后者是单条规则的修订计数。
func TestGatewayHandler_PutAndDeleteReturnSnapshotVersion(t *testing.T) {
	r, _ := newGatewayTestRouter()

	rec := doReq(t, r, http.MethodPut, "/api/paas/gateway-rules/default-agent-service-api", validRuleBody())
	if rec.Code != http.StatusOK {
		t.Fatalf("PUT expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var putEnv struct {
		Data struct {
			Version         int64 `json:"version"`
			SnapshotVersion int64 `json:"snapshot_version"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &putEnv); err != nil {
		t.Fatalf("decode PUT: %v", err)
	}
	if putEnv.Data.SnapshotVersion != 1 {
		t.Errorf("PUT response must carry tx-assigned snapshot_version=1, got %d", putEnv.Data.SnapshotVersion)
	}
	if putEnv.Data.Version != 1 {
		t.Errorf("PUT response must still carry rule version=1, got %d", putEnv.Data.Version)
	}

	rec = doReq(t, r, http.MethodDelete, "/api/paas/gateway-rules/default-agent-service-api", "")
	if rec.Code != http.StatusOK {
		t.Fatalf("DELETE expected 200, got %d", rec.Code)
	}
	var delEnv struct {
		Data struct {
			Deleted         string `json:"deleted"`
			SnapshotVersion int64  `json:"snapshot_version"`
		} `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &delEnv); err != nil {
		t.Fatalf("decode DELETE: %v", err)
	}
	if delEnv.Data.SnapshotVersion != 2 {
		t.Errorf("DELETE response must carry tx-assigned snapshot_version=2, got %d", delEnv.Data.SnapshotVersion)
	}
	if delEnv.Data.Deleted != "default-agent-service-api" {
		t.Errorf("DELETE response must echo deleted name, got %q", delEnv.Data.Deleted)
	}
}
