package domain

import (
	"fmt"
	"regexp"
	"strings"
)

// gatewayNamePattern：lowercase 字母数字开头，后续允许 - 与字母数字。
var gatewayNamePattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]*$`)

// 注意：本包不复用 ClassifyLane —— ClassifyLane 把 blue 视为合法（蓝绿自部署专用），
// 而路由规则禁止引用 blue，故这里用更严格的正则单独放行 prod / ppe-* / coe-*。
var gatewayCoeLanePattern = regexp.MustCompile(`^coe-[a-z0-9][a-z0-9-]*$`)
var gatewayPpeLanePattern = regexp.MustCompile(`^ppe-[a-z0-9][a-z0-9-]*$`)

// isGatewayLane 报告 lane 是否为路由规则允许的值（prod / ppe-* / coe-*，不含 blue）。
func isGatewayLane(lane string) bool {
	return lane == "prod" ||
		gatewayPpeLanePattern.MatchString(lane) ||
		gatewayCoeLanePattern.MatchString(lane)
}

// ValidateGatewayRule 在写入前对一条路由规则做完整校验，挡脏数据进表。
// 返回的 error 一律 wrap ErrInvalidInput，便于 handler 映射为 400。
func ValidateGatewayRule(rule GatewayRule) error {
	if err := validateGatewayName(rule.Name); err != nil {
		return err
	}
	if err := validateGatewayPathPrefix(rule.PathPrefix, rule.Match.PathPrefix); err != nil {
		return err
	}
	if err := validateGatewayRequestLane(rule.RequestLane, rule.Match.RequestLane); err != nil {
		return err
	}
	if err := validateGatewayMatchExtensions(rule.Match); err != nil {
		return err
	}
	if err := validateGatewayTargets(rule.Targets); err != nil {
		return err
	}
	if err := validateGatewayFallback(rule.Fallback); err != nil {
		return err
	}
	return nil
}

func validateGatewayName(name string) error {
	if name == "" {
		return fmt.Errorf("%w: name is required", ErrInvalidInput)
	}
	if len(name) > 64 {
		return fmt.Errorf("%w: name %q exceeds 64 chars", ErrInvalidInput, name)
	}
	if !gatewayNamePattern.MatchString(name) {
		return fmt.Errorf("%w: name %q must match ^[a-z0-9][a-z0-9-]*$", ErrInvalidInput, name)
	}
	return nil
}

func validateGatewayPathPrefix(topLevel, matchLevel string) error {
	if topLevel == "" {
		return fmt.Errorf("%w: path_prefix is required", ErrInvalidInput)
	}
	if !strings.HasPrefix(topLevel, "/") {
		return fmt.Errorf("%w: path_prefix %q must start with '/'", ErrInvalidInput, topLevel)
	}
	// 必须以 '/' 结尾：api-gateway matcher 用 strings.HasPrefix，没有 trailing slash
	// 的前缀（如 /dashboard）会误命中 /dashboard-api 之类路径，破坏前缀语义。
	if !strings.HasSuffix(topLevel, "/") {
		return fmt.Errorf("%w: path_prefix %q must end with '/'", ErrInvalidInput, topLevel)
	}
	if matchLevel != topLevel {
		return fmt.Errorf(
			"%w: match.path_prefix %q must equal top-level path_prefix %q",
			ErrInvalidInput, matchLevel, topLevel,
		)
	}
	return nil
}

func validateGatewayRequestLane(topLevel, matchLevel string) error {
	if topLevel != matchLevel {
		return fmt.Errorf(
			"%w: match.request_lane %q must equal top-level request_lane %q",
			ErrInvalidInput, matchLevel, topLevel,
		)
	}
	if topLevel == "" {
		return nil // request_lane 可空：path 通用规则不约束 lane
	}
	if !isGatewayLane(topLevel) {
		return fmt.Errorf(
			"%w: request_lane %q is not allowed (only prod / ppe-* / coe-*, not blue)",
			ErrInvalidInput, topLevel,
		)
	}
	return nil
}

func validateGatewayMatchExtensions(m GatewayMatch) error {
	if m.Method != "" {
		return fmt.Errorf("%w: match.method is not supported in MVP (二期再开)", ErrInvalidInput)
	}
	if len(m.Headers) > 0 {
		return fmt.Errorf("%w: match.headers is not supported in MVP (二期再开)", ErrInvalidInput)
	}
	if len(m.Query) > 0 {
		return fmt.Errorf("%w: match.query is not supported in MVP (二期再开)", ErrInvalidInput)
	}
	if len(m.Cookies) > 0 {
		return fmt.Errorf("%w: match.cookies is not supported in MVP (二期再开)", ErrInvalidInput)
	}
	return nil
}

func validateGatewayTargets(targets []GatewayTarget) error {
	if len(targets) != 1 {
		return fmt.Errorf("%w: targets must contain exactly 1 entry (MVP 不支持多 target), got %d", ErrInvalidInput, len(targets))
	}
	t := targets[0]
	if t.Service == "" {
		return fmt.Errorf("%w: target.service is required", ErrInvalidInput)
	}
	// target.lane 可空：空 = "跟随请求 x-lane 透传"（api-gateway 运行时行为），
	// paas-engine 视为合法、跳过 lane 命名校验；非空时才校验 prod / ppe-* / coe-*（拒 blue）。
	if t.Lane != "" && !isGatewayLane(t.Lane) {
		return fmt.Errorf(
			"%w: target.lane %q is not allowed (only prod / ppe-* / coe-*, not blue)",
			ErrInvalidInput, t.Lane,
		)
	}
	if t.Port < 1 || t.Port > 65535 {
		return fmt.Errorf("%w: target.port %d must be in [1, 65535]", ErrInvalidInput, t.Port)
	}
	if t.Weight != 100 {
		return fmt.Errorf("%w: target.weight must be 100 in MVP (单 target), got %d", ErrInvalidInput, t.Weight)
	}
	return nil
}

func validateGatewayFallback(f GatewayFallback) error {
	if f.Mode != "prod" && f.Mode != "reject" {
		return fmt.Errorf("%w: fallback.mode %q must be one of {prod, reject}", ErrInvalidInput, f.Mode)
	}
	return nil
}
