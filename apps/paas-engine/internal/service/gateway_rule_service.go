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
	Fallback    domain.GatewayFallback `json:"fallback"`
}

// enabledOrDefault 解析 Enabled 三态：nil（未提供）-> true（规则默认启用）；
// 显式 true/false -> 按值。
func (req UpsertGatewayRuleRequest) enabledOrDefault() bool {
	if req.Enabled == nil {
		return true
	}
	return *req.Enabled
}

// Upsert 校验并写入一条规则（name 幂等 key）。
// 新建时 version=1、设置 created_at；更新时 version+1、保留原 created_at。
func (s *GatewayRuleService) Upsert(ctx context.Context, name string, req UpsertGatewayRuleRequest) error {
	now := time.Now()
	rule := domain.GatewayRule{
		Name:        name,
		Enabled:     req.enabledOrDefault(),
		Priority:    req.Priority,
		PathPrefix:  req.PathPrefix,
		RequestLane: req.RequestLane,
		Match:       req.Match,
		Targets:     req.Targets,
		Fallback:    req.Fallback,
		UpdatedAt:   now,
	}

	if err := domain.ValidateGatewayRule(rule); err != nil {
		return err
	}

	existing, err := s.repo.FindByName(ctx, name)
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

	return s.repo.Upsert(ctx, &rule)
}

// ensureRule 校验后以 insert-do-nothing 写入一条规则（version=1，全新插入语义）。
// name 已存在则 repo 层 DoNothing，原规则一字不动——基线 ensure 专用，绝不覆盖。
func (s *GatewayRuleService) ensureRule(ctx context.Context, name string, req UpsertGatewayRuleRequest) error {
	now := time.Now()
	rule := domain.GatewayRule{
		Name:        name,
		Enabled:     req.enabledOrDefault(),
		Priority:    req.Priority,
		PathPrefix:  req.PathPrefix,
		RequestLane: req.RequestLane,
		Match:       req.Match,
		Targets:     req.Targets,
		Fallback:    req.Fallback,
		Version:     1,
		CreatedAt:   now,
		UpdatedAt:   now,
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

// Delete 删除一条规则。
func (s *GatewayRuleService) Delete(ctx context.Context, name string) error {
	return s.repo.Delete(ctx, name)
}

// Snapshot 组装 /internal/gateway-rules 返回的快照。
// Version 取 max(rule.version)（snapshot 级单调 int）；空表返回 version=0、空规则集。
func (s *GatewayRuleService) Snapshot(ctx context.Context) (*domain.GatewaySnapshot, error) {
	rules, err := s.List(ctx)
	if err != nil {
		return nil, err
	}

	out := make([]domain.GatewayRule, 0, len(rules))
	var maxVersion int64
	var latestUpdate time.Time
	for _, r := range rules {
		out = append(out, *r)
		if r.Version > maxVersion {
			maxVersion = r.Version
		}
		if r.UpdatedAt.After(latestUpdate) {
			latestUpdate = r.UpdatedAt
		}
	}

	return &domain.GatewaySnapshot{
		Version:   maxVersion,
		UpdatedAt: latestUpdate,
		Rules:     out,
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
