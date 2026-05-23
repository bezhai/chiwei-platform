package http

import (
	"encoding/json"
	"net/http"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

// newEmergencyTestRouter wires the full gateway-rules sub-router (including the
// explain / disable / enable / set-weights action endpoints) over a stub repo,
// matching how router.go registers them.
func newEmergencyTestRouter() (*chi.Mux, *gwStubRepo) {
	repo := newGwStubRepo()
	svc := service.NewGatewayRuleService(repo)
	h := NewGatewayRuleHandler(svc)
	r := chi.NewRouter()
	r.Route("/api/paas", func(r chi.Router) {
		r.Post("/gateway-rules:explain", h.Explain)
		r.Route("/gateway-rules", func(r chi.Router) {
			r.Get("/", h.List)
			r.Get("/{name}", h.Get)
			r.Put("/{name}", h.Upsert)
			r.Delete("/{name}", h.Delete)
			r.Post("/{name}:disable", h.Disable)
			r.Post("/{name}:enable", h.Enable)
			r.Post("/{name}:set-weights", h.SetWeights)
		})
	})
	return r, repo
}

func seedTwoTargetRule(repo *gwStubRepo) {
	repo.rules["agent"] = &domain.GatewayRule{
		Name:       "agent",
		Enabled:    true,
		Priority:   100,
		PathPrefix: "/api/agent/",
		Match:      domain.GatewayMatch{PathPrefix: "/api/agent/"},
		Targets: []domain.GatewayTarget{
			{Service: "agent-service", Lane: "prod", Port: 8000, Weight: 90},
			{Service: "agent-service", Lane: "ppe-new", Port: 8000, Weight: 10},
		},
		Version: 1,
	}
}

func TestExplainEndpointReturnsTrace(t *testing.T) {
	r, repo := newEmergencyTestRouter()
	seedTwoTargetRule(repo)

	rec := doReq(t, r, http.MethodPost, "/api/paas/gateway-rules:explain",
		`{"path":"/api/agent/health","x_lane":""}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("explain expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var env struct {
		Data domain.GatewayExplainResult `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode explain: %v", err)
	}
	if !env.Data.Matched || env.Data.WinningRule != "agent" {
		t.Errorf("explain matched=%v winner=%q", env.Data.Matched, env.Data.WinningRule)
	}
	if len(env.Data.CandidateTargets) != 2 {
		t.Errorf("expected 2 candidate targets, got %d", len(env.Data.CandidateTargets))
	}
}

func TestDisableEndpoint(t *testing.T) {
	r, repo := newEmergencyTestRouter()
	seedTwoTargetRule(repo)

	rec := doReq(t, r, http.MethodPost, "/api/paas/gateway-rules/agent:disable",
		`{"reason":"incident-1"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("disable expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var env struct {
		Data service.EnableChange `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode disable: %v", err)
	}
	if env.Data.BeforeEnabled != true || env.Data.AfterEnabled != false {
		t.Errorf("before/after = %v/%v want true/false", env.Data.BeforeEnabled, env.Data.AfterEnabled)
	}
	if env.Data.Reason != "incident-1" {
		t.Errorf("reason=%q", env.Data.Reason)
	}
	if repo.rules["agent"].Enabled {
		t.Error("rule still enabled in repo")
	}
}

func TestEnableEndpoint(t *testing.T) {
	r, repo := newEmergencyTestRouter()
	seedTwoTargetRule(repo)
	repo.rules["agent"].Enabled = false

	rec := doReq(t, r, http.MethodPost, "/api/paas/gateway-rules/agent:enable",
		`{"reason":"recovered"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("enable expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	if !repo.rules["agent"].Enabled {
		t.Error("rule not enabled after enable endpoint")
	}
}

func TestSetWeightsEndpoint(t *testing.T) {
	r, repo := newEmergencyTestRouter()
	seedTwoTargetRule(repo)

	body := `{"reason":"drain ppe-new","weights":[
		{"service":"agent-service","lane":"prod","weight":100},
		{"service":"agent-service","lane":"ppe-new","weight":0}]}`
	rec := doReq(t, r, http.MethodPost, "/api/paas/gateway-rules/agent:set-weights", body)
	if rec.Code != http.StatusOK {
		t.Fatalf("set-weights expected 200, got %d: %s", rec.Code, rec.Body.String())
	}
	var env struct {
		Data service.WeightsChange `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode set-weights: %v", err)
	}
	if len(env.Data.BeforeTargets) != 2 || len(env.Data.AfterTargets) != 2 {
		t.Fatalf("before/after target count wrong")
	}
	for _, tg := range repo.rules["agent"].Targets {
		if tg.Lane == "ppe-new" && tg.Weight != 0 {
			t.Errorf("ppe-new weight=%d want 0", tg.Weight)
		}
	}
}

func TestSetWeightsEndpointRejectsBadSum(t *testing.T) {
	r, repo := newEmergencyTestRouter()
	seedTwoTargetRule(repo)

	body := `{"reason":"x","weights":[
		{"service":"agent-service","lane":"prod","weight":50},
		{"service":"agent-service","lane":"ppe-new","weight":40}]}`
	rec := doReq(t, r, http.MethodPost, "/api/paas/gateway-rules/agent:set-weights", body)
	if rec.Code != http.StatusBadRequest {
		t.Fatalf("expected 400 for bad sum, got %d: %s", rec.Code, rec.Body.String())
	}
}

func TestDisableEndpointMissingRule404(t *testing.T) {
	r, _ := newEmergencyTestRouter()
	rec := doReq(t, r, http.MethodPost, "/api/paas/gateway-rules/nope:disable", `{"reason":"x"}`)
	if rec.Code != http.StatusNotFound {
		t.Fatalf("expected 404, got %d", rec.Code)
	}
}
