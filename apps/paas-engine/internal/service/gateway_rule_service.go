package service

import (
	"context"
	"errors"
	"sort"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

// GatewayRuleService 持有 api-gateway 动态路由规则，写入前跑完整校验，
// 并组装 /internal/gateway-rules 消费的快照。
type GatewayRuleService struct {
	repo port.GatewayRuleRepository
}

func NewGatewayRuleService(repo port.GatewayRuleRepository) *GatewayRuleService {
	return &GatewayRuleService{repo: repo}
}

// UpsertGatewayRuleRequest 是 PUT /api/paas/gateway-rules/{name} 的请求体。
// Name 不在请求体里——由 URL path 提供做幂等 key。
type UpsertGatewayRuleRequest struct {
	// Enabled 用 *bool 区分"未提供"和"显式 false"：PUT body 省略 enabled 时
	// Go 会把普通 bool 填成 false，导致创建出"写入成功但 enabled=false 不生效"
	// 的规则（运维 footgun）。指针为 nil 时按 enabledOrDefault() 默认启用。
	Enabled     *bool                  `json:"enabled"`
	Priority    int                    `json:"priority"`
	PathPrefix  string                 `json:"path_prefix"`
	RequestLane string                 `json:"request_lane"`
	Match       domain.GatewayMatch    `json:"match"`
	Targets     []domain.GatewayTarget `json:"targets"`
	// SplitKeyHeaders configures stable (sticky) target selection downstream;
	// nil/empty means weighted-random. Validated as HTTP header names.
	SplitKeyHeaders []string `json:"split_key_headers"`
	// Reason 是本次写操作的运维原因，落进快照历史的 reason 字段（供审计与回滚追溯）。
	Reason string `json:"reason"`
}

// snapshotActor 是写快照时记录的 created_by 默认值。规则写操作都经 ops/Dashboard
// 安全入口进来，真实调用方由 Dashboard audit 中间件单独记录；这里记一个来源标签
// 表明快照由 ops 写操作产生。
const snapshotActor = "ops"

// recordSnapshot 读取当前完整规则集，在 txRepo（事务作用域）内分配下一个独立单调
// snapshot_version 并落一条历史，返回分配到的版本号。所有写操作在同一事务里调它，
// 保证"改规则 + 写快照"原子完成。
//
// 已知限制（接受风险、不加锁）：repo.Tx 是 Postgres 默认 READ COMMITTED、无写串行化。
// 两个 gateway 规则写事务毫秒级重叠时，后提交者可能拿到更高的 snapshot_version 却没
// FindAll 到先提交者的改动，导致"最高版本快照滞后于实际规则表"、回滚到最新版会丢那条
// 改动。不加 advisory lock 是因为 gateway 规则是纯运维写、几乎天然串行，并发窗口极小；
// 若未来写频升高（多副本自动化改规则）需重新评估，届时在此处加 pg_advisory_xact_lock。
func recordSnapshot(ctx context.Context, txRepo port.GatewayRuleRepository, reason string) (int64, error) {
	rules, err := txRepo.FindAll(ctx)
	if err != nil {
		return 0, err
	}
	full := make([]domain.GatewayRule, 0, len(rules))
	for _, r := range rules {
		full = append(full, *r)
	}
	return txRepo.SaveSnapshot(ctx, full, snapshotActor, reason)
}

// enabledOrDefault 解析 Enabled 三态：nil（未提供）-> true（规则默认启用）；
// 显式 true/false -> 按值。
func (req UpsertGatewayRuleRequest) enabledOrDefault() bool {
	if req.Enabled == nil {
		return true
	}
	return *req.Enabled
}

// Upsert 校验并写入一条规则（name 幂等 key），返回本次事务分配的 snapshot_version。
// 新建时 version=1、设置 created_at；更新时 version+1、保留原 created_at。
// 返回的 snapshot_version 是审计/回滚游标，与规则自身的 rule.Version 是两套版本号。
func (s *GatewayRuleService) Upsert(ctx context.Context, name string, req UpsertGatewayRuleRequest) (int64, error) {
	now := time.Now()
	rule := domain.GatewayRule{
		Name:            name,
		Enabled:         req.enabledOrDefault(),
		Priority:        req.Priority,
		PathPrefix:      req.PathPrefix,
		RequestLane:     req.RequestLane,
		Match:           req.Match,
		Targets:         req.Targets,
		SplitKeyHeaders: req.SplitKeyHeaders,
		UpdatedAt:       now,
	}

	if err := domain.ValidateGatewayRule(rule); err != nil {
		return 0, err
	}

	var snapVersion int64
	err := s.repo.Tx(ctx, func(txRepo port.GatewayRuleRepository) error {
		existing, err := txRepo.FindByName(ctx, name)
		switch {
		case err == nil:
			rule.CreatedAt = existing.CreatedAt
			rule.Version = existing.Version + 1
		case errors.Is(err, domain.ErrGatewayRuleNotFound):
			rule.CreatedAt = now
			rule.Version = 1
		default:
			return err
		}
		if err := txRepo.Upsert(ctx, &rule); err != nil {
			return err
		}
		snapVersion, err = recordSnapshot(ctx, txRepo, req.Reason)
		return err
	})
	if err != nil {
		return 0, err
	}
	return snapVersion, nil
}

// ensureRule 校验后以 insert-do-nothing 写入一条规则（version=1，全新插入语义）。
// name 已存在则 repo 层 DoNothing，原规则一字不动——基线 ensure 专用，绝不覆盖。
func (s *GatewayRuleService) ensureRule(ctx context.Context, name string, req UpsertGatewayRuleRequest) error {
	now := time.Now()
	rule := domain.GatewayRule{
		Name:            name,
		Enabled:         req.enabledOrDefault(),
		Priority:        req.Priority,
		PathPrefix:      req.PathPrefix,
		RequestLane:     req.RequestLane,
		Match:           req.Match,
		Targets:         req.Targets,
		SplitKeyHeaders: req.SplitKeyHeaders,
		Version:         1,
		CreatedAt:       now,
		UpdatedAt:       now,
	}
	if err := domain.ValidateGatewayRule(rule); err != nil {
		return err
	}
	return s.repo.InsertIfAbsent(ctx, &rule)
}

// Get 返回单条规则。
func (s *GatewayRuleService) Get(ctx context.Context, name string) (*domain.GatewayRule, error) {
	return s.repo.FindByName(ctx, name)
}

// List 返回全部规则，按 priority desc、path_prefix 长度 desc 排序。
func (s *GatewayRuleService) List(ctx context.Context) ([]*domain.GatewayRule, error) {
	rules, err := s.repo.FindAll(ctx)
	if err != nil {
		return nil, err
	}
	if rules == nil {
		rules = []*domain.GatewayRule{}
	}
	sortGatewayRules(rules)
	return rules, nil
}

// Delete 删除一条规则，并在同一事务内写一条快照历史。
// 含 delete 是关键：删掉 version 最高的规则后，独立 snapshot_version 仍单调前进，
// 不像 max(rule.version) 那样回退。
func (s *GatewayRuleService) Delete(ctx context.Context, name, reason string) (int64, error) {
	var snapVersion int64
	err := s.repo.Tx(ctx, func(txRepo port.GatewayRuleRepository) error {
		if err := txRepo.Delete(ctx, name); err != nil {
			return err
		}
		var err error
		snapVersion, err = recordSnapshot(ctx, txRepo, reason)
		return err
	})
	if err != nil {
		return 0, err
	}
	return snapVersion, nil
}

// Explain 预览一个请求会命中哪条规则、为何命中、其余规则为何没命中。
// 取全部规则交给 domain.ExplainGatewayMatch（与 api-gateway matcher 对齐的纯逻辑）。
func (s *GatewayRuleService) Explain(ctx context.Context, path, requestLane string) (*domain.GatewayExplainResult, error) {
	rules, err := s.repo.FindAll(ctx)
	if err != nil {
		return nil, err
	}
	res := domain.ExplainGatewayMatch(rules, path, requestLane)
	return &res, nil
}

// Snapshot 组装 /internal/gateway-rules 返回的快照。
// Version 取最新的独立 snapshot_version（来自快照序列，而非 max(rule.version)）——
// 这样删掉 version 最高的规则也不会回退。空历史返回 version=0。
func (s *GatewayRuleService) Snapshot(ctx context.Context) (*domain.GatewaySnapshot, error) {
	rules, err := s.List(ctx)
	if err != nil {
		return nil, err
	}

	version, err := s.repo.LatestSnapshotVersion(ctx)
	if err != nil {
		return nil, err
	}

	out := make([]domain.GatewayRule, 0, len(rules))
	var latestUpdate time.Time
	for _, r := range rules {
		out = append(out, *r)
		if r.UpdatedAt.After(latestUpdate) {
			latestUpdate = r.UpdatedAt
		}
	}

	return &domain.GatewaySnapshot{
		Version:   version,
		UpdatedAt: latestUpdate,
		Rules:     out,
	}, nil
}

// ListSnapshots 返回最近 limit 条规则快照历史（按 snapshot_version 倒序）。
func (s *GatewayRuleService) ListSnapshots(ctx context.Context, limit int) ([]*domain.GatewayRuleSnapshot, error) {
	snaps, err := s.repo.ListSnapshots(ctx, limit)
	if err != nil {
		return nil, err
	}
	if snaps == nil {
		snaps = []*domain.GatewayRuleSnapshot{}
	}
	return snaps, nil
}

// GetSnapshot 取单条历史快照。
func (s *GatewayRuleService) GetSnapshot(ctx context.Context, version int64) (*domain.GatewayRuleSnapshot, error) {
	return s.repo.GetSnapshot(ctx, version)
}

// Rollback 把历史某版本的规则集重新写入当前 gateway_routing_rules，并在同一事务内
// 分配一个更大的新 snapshot_version 写一条新快照——不是把版本号倒回去。
// 当前表里有、目标快照里没有的规则会被删掉，使当前规则集与目标版本完全一致。
func (s *GatewayRuleService) Rollback(ctx context.Context, version int64, reason string) (*domain.GatewayRuleSnapshot, error) {
	var newVersion int64
	err := s.repo.Tx(ctx, func(txRepo port.GatewayRuleRepository) error {
		target, err := txRepo.GetSnapshot(ctx, version)
		if err != nil {
			return err
		}

		// 目标版本里应存在的规则名集合。
		wanted := make(map[string]bool, len(target.Rules))
		for _, r := range target.Rules {
			wanted[r.Name] = true
		}

		// 删掉当前存在但目标版本没有的规则。
		current, err := txRepo.FindAll(ctx)
		if err != nil {
			return err
		}
		for _, r := range current {
			if !wanted[r.Name] {
				if err := txRepo.Delete(ctx, r.Name); err != nil {
					return err
				}
			}
		}

		// 把目标版本每条规则覆盖回当前表（保留其历史 version 字段值，按原样恢复）。
		for i := range target.Rules {
			rule := target.Rules[i]
			if err := txRepo.Upsert(ctx, &rule); err != nil {
				return err
			}
		}

		newVersion, err = recordSnapshot(ctx, txRepo, reason)
		return err
	})
	if err != nil {
		return nil, err
	}
	return &domain.GatewayRuleSnapshot{
		SnapshotVersion: newVersion,
		Reason:          reason,
		CreatedBy:       snapshotActor,
	}, nil
}

// sortGatewayRules 按 matcher 优先级排序：priority desc，平手时 path_prefix 长度 desc，
// 再平手按 name asc 保证稳定。
func sortGatewayRules(rules []*domain.GatewayRule) {
	sort.SliceStable(rules, func(i, j int) bool {
		if rules[i].Priority != rules[j].Priority {
			return rules[i].Priority > rules[j].Priority
		}
		li, lj := len(rules[i].PathPrefix), len(rules[j].PathPrefix)
		if li != lj {
			return li > lj
		}
		return rules[i].Name < rules[j].Name
	})
}
