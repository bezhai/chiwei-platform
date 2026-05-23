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
	if err := validateGatewaySplitKeyHeaders(rule.SplitKeyHeaders); err != nil {
		return err
	}
	return nil
}

// httpHeaderNamePattern matches an RFC 7230 header field-name (a token): one or
// more token characters, no spaces / colons / control characters.
var httpHeaderNamePattern = regexp.MustCompile(`^[A-Za-z0-9!#$%&'*+\-.^_` + "`" + `|~]+$`)

// validateGatewaySplitKeyHeaders accepts an empty/nil list (no stable split);
// when non-empty, every element must be a valid HTTP header name.
func validateGatewaySplitKeyHeaders(headers []string) error {
	for i, h := range headers {
		if h == "" {
			return fmt.Errorf("%w: split_key_headers[%d] must not be empty", ErrInvalidInput, i)
		}
		if !httpHeaderNamePattern.MatchString(h) {
			return fmt.Errorf(
				"%w: split_key_headers[%d] %q is not a valid HTTP header name",
				ErrInvalidInput, i, h,
			)
		}
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

// validateGatewayTargets 放开多 target 加权分流：至少 1 个 target，
// 每个 target 校验 service / lane / port，且全部 weight 之和必须等于 100。
// 单个 target 权重 0 合法（用于把流量从某 target 撤走），负数拒绝。
func validateGatewayTargets(targets []GatewayTarget) error {
	if len(targets) == 0 {
		return fmt.Errorf("%w: targets must contain at least 1 entry, got 0", ErrInvalidInput)
	}
	weightSum := 0
	type targetIdentity struct{ service, lane string }
	seen := make(map[targetIdentity]struct{}, len(targets))
	for i := range targets {
		t := targets[i]
		if t.Service == "" {
			return fmt.Errorf("%w: target[%d].service is required", ErrInvalidInput, i)
		}
		// service+lane 是 target 身份（set-weights 据此定位 target），规则内必须唯一。
		id := targetIdentity{t.Service, t.Lane}
		if _, dup := seen[id]; dup {
			return fmt.Errorf(
				"%w: target[%d] has duplicate identity service=%q lane=%q (service+lane must be unique within a rule)",
				ErrInvalidInput, i, t.Service, t.Lane,
			)
		}
		seen[id] = struct{}{}
		// target.lane 可空：空 = "跟随请求 x-lane 透传"（api-gateway 运行时行为），
		// paas-engine 视为合法、跳过 lane 命名校验；非空时才校验 prod / ppe-* / coe-*（拒 blue）。
		if t.Lane != "" && !isGatewayLane(t.Lane) {
			return fmt.Errorf(
				"%w: target[%d].lane %q is not allowed (only prod / ppe-* / coe-*, not blue)",
				ErrInvalidInput, i, t.Lane,
			)
		}
		if t.Port < 1 || t.Port > 65535 {
			return fmt.Errorf("%w: target[%d].port %d must be in [1, 65535]", ErrInvalidInput, i, t.Port)
		}
		if t.Weight < 0 {
			return fmt.Errorf("%w: target[%d].weight %d must not be negative", ErrInvalidInput, i, t.Weight)
		}
		weightSum += t.Weight
	}
	if weightSum != 100 {
		return fmt.Errorf("%w: target weight sum must be 100, got %d", ErrInvalidInput, weightSum)
	}
	return nil
}
