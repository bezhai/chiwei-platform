package domain

import (
	"errors"
	"strings"
	"testing"
)

// validRule returns a minimal rule that passes every validation rule.
// Tests mutate a copy to exercise individual reject conditions.
func validRule() GatewayRule {
	return GatewayRule{
		Name:        "default-agent-service-api",
		Enabled:     true,
		Priority:    100,
		PathPrefix:  "/api/agent/",
		RequestLane: "",
		Match: GatewayMatch{
			PathPrefix: "/api/agent/",
		},
		Targets: []GatewayTarget{
			{
				Service: "agent-service",
				Lane:    "prod",
				Port:    8000,
				Weight:  100,
			},
		},
		Fallback: GatewayFallback{Mode: "prod"},
	}
}

func TestValidateGatewayRule_Valid(t *testing.T) {
	if err := ValidateGatewayRule(validRule()); err != nil {
		t.Fatalf("expected valid rule to pass, got: %v", err)
	}
}

func TestValidateGatewayRule_ValidWithPpeRequestLane(t *testing.T) {
	r := validRule()
	r.RequestLane = "ppe-foo"
	r.Match.RequestLane = "ppe-foo"
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("expected ppe request_lane to pass, got: %v", err)
	}
}

func TestValidateGatewayRule_ValidWithCoeTargetLane(t *testing.T) {
	r := validRule()
	r.Targets[0].Lane = "coe-bar"
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("expected coe target lane to pass, got: %v", err)
	}
}

func TestValidateGatewayRule_ValidWithStripPrefix(t *testing.T) {
	r := validRule()
	r.Targets[0].StripPrefix = "/api/agent"
	r.Targets[0].RewritePrefix = "/v2"
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("expected strip/rewrite prefix to pass, got: %v", err)
	}
}

func assertReject(t *testing.T, r GatewayRule, wantSubstr string) {
	t.Helper()
	err := ValidateGatewayRule(r)
	if err == nil {
		t.Fatalf("expected reject containing %q, got nil", wantSubstr)
	}
	if !errors.Is(err, ErrInvalidInput) {
		t.Fatalf("expected ErrInvalidInput, got: %v", err)
	}
	if wantSubstr != "" && !strings.Contains(err.Error(), wantSubstr) {
		t.Fatalf("expected error to contain %q, got: %v", wantSubstr, err)
	}
}

func TestValidateGatewayRule_NameEmpty(t *testing.T) {
	r := validRule()
	r.Name = ""
	assertReject(t, r, "name")
}

func TestValidateGatewayRule_NameTooLong(t *testing.T) {
	r := validRule()
	r.Name = strings.Repeat("a", 65)
	assertReject(t, r, "name")
}

func TestValidateGatewayRule_NameInvalidChars(t *testing.T) {
	for _, bad := range []string{"Default", "-leading", "has_underscore", "has space", "café"} {
		r := validRule()
		r.Name = bad
		assertReject(t, r, "name")
	}
}

func TestValidateGatewayRule_NameMaxLengthOK(t *testing.T) {
	r := validRule()
	r.Name = strings.Repeat("a", 64)
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("expected 64-char name to pass, got: %v", err)
	}
}

func TestValidateGatewayRule_PathPrefixNoSlash(t *testing.T) {
	r := validRule()
	r.PathPrefix = "api/agent/"
	r.Match.PathPrefix = "api/agent/"
	assertReject(t, r, "path_prefix")
}

func TestValidateGatewayRule_PathPrefixNoTrailingSlash(t *testing.T) {
	// path_prefix 必须以 '/' 结尾，否则 api-gateway 的 HasPrefix 会误命中
	// （如 /dashboard 误中 /dashboard-api），破坏前缀语义。
	r := validRule()
	r.PathPrefix = "/api/agent"
	r.Match.PathPrefix = "/api/agent"
	assertReject(t, r, "path_prefix")
}

func TestValidateGatewayRule_PathPrefixRootSlashOK(t *testing.T) {
	// 单个 "/" 既以 / 开头又以 / 结尾，合法。
	r := validRule()
	r.PathPrefix = "/"
	r.Match.PathPrefix = "/"
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("expected root '/' path_prefix to pass, got: %v", err)
	}
}

func TestValidateGatewayRule_PathPrefixEmpty(t *testing.T) {
	r := validRule()
	r.PathPrefix = ""
	r.Match.PathPrefix = ""
	assertReject(t, r, "path_prefix")
}

func TestValidateGatewayRule_MatchPathPrefixMismatch(t *testing.T) {
	r := validRule()
	r.Match.PathPrefix = "/different/"
	assertReject(t, r, "path_prefix")
}

func TestValidateGatewayRule_RequestLaneBlue(t *testing.T) {
	r := validRule()
	r.RequestLane = "blue"
	r.Match.RequestLane = "blue"
	assertReject(t, r, "request_lane")
}

func TestValidateGatewayRule_RequestLaneInvalid(t *testing.T) {
	r := validRule()
	r.RequestLane = "dev"
	r.Match.RequestLane = "dev"
	assertReject(t, r, "request_lane")
}

func TestValidateGatewayRule_RequestLaneTopLevelMismatch(t *testing.T) {
	r := validRule()
	r.RequestLane = "ppe-foo"
	r.Match.RequestLane = "ppe-bar"
	assertReject(t, r, "request_lane")
}

func TestValidateGatewayRule_TargetsEmpty(t *testing.T) {
	r := validRule()
	r.Targets = nil
	assertReject(t, r, "targets")
}

func TestValidateGatewayRule_TargetsMultiple(t *testing.T) {
	r := validRule()
	r.Targets = append(r.Targets, GatewayTarget{Service: "x", Lane: "prod", Port: 80, Weight: 100})
	assertReject(t, r, "targets")
}

func TestValidateGatewayRule_TargetWeightNot100(t *testing.T) {
	r := validRule()
	r.Targets[0].Weight = 50
	assertReject(t, r, "weight")
}

func TestValidateGatewayRule_TargetServiceEmpty(t *testing.T) {
	r := validRule()
	r.Targets[0].Service = ""
	assertReject(t, r, "service")
}

func TestValidateGatewayRule_TargetLaneEmptyOK(t *testing.T) {
	// 空 lane = "跟随请求 x-lane 透传"，paas-engine 校验视为合法。
	r := validRule()
	r.Targets[0].Lane = ""
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("expected empty target lane to pass (passthrough), got: %v", err)
	}
}

func TestValidateGatewayRule_TargetLaneBlue(t *testing.T) {
	// 非空时仍校验：blue 被拒。
	r := validRule()
	r.Targets[0].Lane = "blue"
	assertReject(t, r, "lane")
}

func TestValidateGatewayRule_TargetLaneInvalid(t *testing.T) {
	r := validRule()
	r.Targets[0].Lane = "staging"
	assertReject(t, r, "lane")
}

func TestValidateGatewayRule_TargetPortZero(t *testing.T) {
	r := validRule()
	r.Targets[0].Port = 0
	assertReject(t, r, "port")
}

func TestValidateGatewayRule_TargetPortTooHigh(t *testing.T) {
	r := validRule()
	r.Targets[0].Port = 65536
	assertReject(t, r, "port")
}

func TestValidateGatewayRule_TargetPortBoundaries(t *testing.T) {
	for _, p := range []int{1, 65535} {
		r := validRule()
		r.Targets[0].Port = p
		if err := ValidateGatewayRule(r); err != nil {
			t.Fatalf("expected port %d to pass, got: %v", p, err)
		}
	}
}

func TestValidateGatewayRule_FallbackModeInvalid(t *testing.T) {
	r := validRule()
	r.Fallback.Mode = "target"
	assertReject(t, r, "fallback")
}

func TestValidateGatewayRule_FallbackModeReject(t *testing.T) {
	r := validRule()
	r.Fallback.Mode = "reject"
	if err := ValidateGatewayRule(r); err != nil {
		t.Fatalf("expected fallback=reject to pass, got: %v", err)
	}
}

func TestValidateGatewayRule_MatchMethodRejected(t *testing.T) {
	r := validRule()
	r.Match.Method = "GET"
	assertReject(t, r, "MVP")
}

func TestValidateGatewayRule_MatchHeadersRejected(t *testing.T) {
	r := validRule()
	r.Match.Headers = map[string]string{"X-Foo": "bar"}
	assertReject(t, r, "MVP")
}

func TestValidateGatewayRule_MatchQueryRejected(t *testing.T) {
	r := validRule()
	r.Match.Query = map[string]string{"a": "b"}
	assertReject(t, r, "MVP")
}

func TestValidateGatewayRule_MatchCookiesRejected(t *testing.T) {
	r := validRule()
	r.Match.Cookies = map[string]string{"sid": "x"}
	assertReject(t, r, "MVP")
}
