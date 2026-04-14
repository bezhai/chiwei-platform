package service

import (
	"context"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type DynamicConfigService struct {
	repo port.DynamicConfigRepository
}

func NewDynamicConfigService(repo port.DynamicConfigRepository) *DynamicConfigService {
	return &DynamicConfigService{repo: repo}
}

// ResolvedEntry 是解析后的单条配置，带来源 lane 标注。
type ResolvedEntry struct {
	Value string `json:"value"`
	Lane  string `json:"lane"`
}

// ResolvedConfig 是解析后的全量配置快照。
type ResolvedConfig struct {
	Configs    map[string]ResolvedEntry `json:"configs"`
	ResolvedAt time.Time               `json:"resolved_at"`
}

// Resolve 返回指定泳道的合并配置（lane 覆盖 + prod 补缺）。
func (s *DynamicConfigService) Resolve(ctx context.Context, lane string) (*ResolvedConfig, error) {
	if lane == "" {
		lane = "prod"
	}

	// 先取 prod 基线
	prodConfigs, err := s.repo.FindByLane(ctx, "prod")
	if err != nil {
		return nil, err
	}

	result := make(map[string]ResolvedEntry, len(prodConfigs))
	for _, c := range prodConfigs {
		result[c.Key] = ResolvedEntry{Value: c.Value, Lane: "prod"}
	}

	// 非 prod 泳道：用该泳道的值覆盖
	if lane != "prod" {
		laneConfigs, err := s.repo.FindByLane(ctx, lane)
		if err != nil {
			return nil, err
		}
		for _, c := range laneConfigs {
			result[c.Key] = ResolvedEntry{Value: c.Value, Lane: lane}
		}
	}

	return &ResolvedConfig{
		Configs:    result,
		ResolvedAt: time.Now(),
	}, nil
}

// List 返回所有配置（可选按 lane 筛选）。
func (s *DynamicConfigService) List(ctx context.Context, lane string) ([]*domain.DynamicConfig, error) {
	if lane != "" {
		return s.repo.FindByLane(ctx, lane)
	}
	return s.repo.FindAll(ctx)
}

// SetDynamicConfigRequest 是设置配置的请求体。
type SetDynamicConfigRequest struct {
	Lane  string `json:"lane"`
	Value string `json:"value"`
}

// Set 设置一条配置（upsert 语义）。
func (s *DynamicConfigService) Set(ctx context.Context, key string, req SetDynamicConfigRequest) error {
	if key == "" {
		return domain.ErrInvalidInput
	}
	lane := req.Lane
	if lane == "" {
		lane = "prod"
	}
	return s.repo.Upsert(ctx, &domain.DynamicConfig{
		Key:       key,
		Lane:      lane,
		Value:     req.Value,
		UpdatedAt: time.Now(),
	})
}

// Delete 删除配置。lane 为空则删除所有 lane 的该 key。
func (s *DynamicConfigService) Delete(ctx context.Context, key, lane string) error {
	if lane != "" {
		return s.repo.DeleteByKeyAndLane(ctx, key, lane)
	}
	return s.repo.DeleteByKey(ctx, key)
}
