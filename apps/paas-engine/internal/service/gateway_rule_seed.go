package service

import (
	"context"
	"fmt"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// BaselineGatewayRule 是一条基线种子规则：name + 对应的 Upsert 请求体。
type BaselineGatewayRule struct {
	Name    string
	Request UpsertGatewayRuleRequest
}

// BaselineGatewayRules 返回 api-gateway 的系统基线路由规则。
//
// 这些规则是系统基线——api-gateway 没有它们业务路径会全断，所以由 paas-engine
// 在启动时幂等 ensure 保证存在，不依赖人工记得灌。
//
// 关键约定：普通业务路径 target.lane 留空，表示"跟随请求 x-lane 透传"。
// webhook 入口强制指向 prod channel-server，lane 决策只能在 channel-server 内部发生。
// lane-bindings 也固定打到 prod channel-server，保证绑定变更清的是入口决策点缓存。
func BaselineGatewayRules() []BaselineGatewayRule {
	enabled := true
	rule := func(name, prefix, service string, port int, stripPrefix string, lane string) BaselineGatewayRule {
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
						Service:     service,
						Lane:        lane,
						Port:        port,
						Weight:      100,
						StripPrefix: stripPrefix,
					},
				},
			},
		}
	}

	return []BaselineGatewayRule{
		rule("default-paas-engine-api", "/api/paas/", "paas-engine", 8080, "", ""),
		rule("default-channel-server-webhook", "/webhook/", "channel-server", 3000, "", "prod"),
		rule("default-channel-server-lane-bindings", "/api/lane-bindings/", "channel-server", 3000, "", "prod"),
		rule("default-agent-service-api", "/api/agent/", "agent-service", 8000, "/api/agent", ""),
		rule("default-monitor-dashboard-api", "/dashboard/api/", "monitor-dashboard", 3002, "", ""),
		rule("default-monitor-dashboard-web", "/dashboard/", "monitor-dashboard-web", 80, "", ""),
	}
}

// EnsureBaseline 幂等地确保基线规则存在：by name 不存在才插入，已存在则不动。
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

	// 启动期 ensure 走 insert-do-nothing，本身不写快照（否则每次重启都灌一条历史）。
	// 但 snapshot_version 是 api-gateway 拉取快照的唯一版本来源——若历史为空（全新库
	// 首启），补写一条 bootstrap 快照让基线规则有 version>0；已有历史则不动。
	version, err := s.repo.LatestSnapshotVersion(ctx)
	if err != nil {
		return fmt.Errorf("read latest snapshot version: %w", err)
	}
	if version == 0 {
		if err := s.repo.Tx(ctx, func(txRepo port.GatewayRuleRepository) error {
			_, err := recordSnapshot(ctx, txRepo, "bootstrap baseline rules")
			return err
		}); err != nil {
			return fmt.Errorf("bootstrap baseline snapshot: %w", err)
		}
	}
	return nil
}
