package service

import (
	"context"
	"fmt"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// BaselineGatewayRule 是一条基线种子规则：name + 对应的 Upsert 请求体。
type BaselineGatewayRule struct {
	Name    string
	Request UpsertGatewayRuleRequest
}

// BaselineGatewayRules 返回 api-gateway 的 6 条系统基线路由规则，从历史
// apps/api-gateway/config/routes.yaml 1:1 平迁而来。
//
// 这 6 条是系统基线——api-gateway 没有它们业务路径会全断，所以由 paas-engine
// 在启动时幂等 ensure 保证存在，不依赖人工记得灌。
//
// 关键约定：所有 target.lane 全部留空——空表示"跟随请求 x-lane 透传"，
// 跟当前 routes.yaml 的泳道路由行为完全一致，绝不写死 prod。
func BaselineGatewayRules() []BaselineGatewayRule {
	enabled := true
	rule := func(name, prefix, service string, port int, stripPrefix string) BaselineGatewayRule {
		return BaselineGatewayRule{
			Name: name,
			Request: UpsertGatewayRuleRequest{
				Enabled:    &enabled,
				Priority:   100,
				PathPrefix: prefix,
				// request_lane 留空：path 通用规则不约束来源 lane。
				RequestLane: "",
				Match: domain.GatewayMatch{
					PathPrefix: prefix,
				},
				Targets: []domain.GatewayTarget{
					{
						Service: service,
						// lane 留空 = 跟随请求 x-lane 透传（平迁现状）。
						Lane:        "",
						Port:        port,
						Weight:      100,
						StripPrefix: stripPrefix,
					},
				},
			},
		}
	}

	return []BaselineGatewayRule{
		rule("default-paas-engine-api", "/api/paas/", "paas-engine", 8080, ""),
		rule("default-channel-proxy-lark", "/api/lark/", "channel-proxy", 3003, ""),
		rule("default-channel-proxy-webhook", "/webhook/", "channel-proxy", 3003, ""),
		rule("default-agent-service-api", "/api/agent/", "agent-service", 8000, "/api/agent"),
		rule("default-monitor-dashboard-api", "/dashboard/api/", "monitor-dashboard", 3002, ""),
		rule("default-monitor-dashboard-web", "/dashboard/", "monitor-dashboard-web", 80, ""),
	}
}

// EnsureBaseline 幂等地确保 6 条基线规则存在：by name 不存在才插入，已存在则不动。
//
// 幂等语义（保护落在 repo 层的 InsertIfAbsent / OnConflict DoNothing）：
//   - 不存在 -> 插入（version=1）
//   - 已存在 -> DB 层 DoNothing，一字不覆盖（人工改过的规则不会被重启冲掉）
//
// 直接走 ensureRule（insert-do-nothing + 完整校验），不再靠 service 层 FindByName
// 预判——后者存在 TOCTOU 窗口且语义上能覆盖并发插入，与"已存在则不动"承诺不自洽。
func (s *GatewayRuleService) EnsureBaseline(ctx context.Context) error {
	for _, seed := range BaselineGatewayRules() {
		if err := s.ensureRule(ctx, seed.Name, seed.Request); err != nil {
			return fmt.Errorf("ensure baseline rule %q: %w", seed.Name, err)
		}
	}
	return nil
}
