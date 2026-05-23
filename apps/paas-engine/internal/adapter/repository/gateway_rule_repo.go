package repository

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

var _ port.GatewayRuleRepository = (*GatewayRuleRepo)(nil)

type GatewayRuleRepo struct {
	db *gorm.DB
}

func NewGatewayRuleRepo(db *gorm.DB) *GatewayRuleRepo {
	return &GatewayRuleRepo{db: db}
}

// Upsert 以 name 为冲突 key 写入；service 层已算好 version / created_at。
func (r *GatewayRuleRepo) Upsert(ctx context.Context, rule *domain.GatewayRule) error {
	m, err := gatewayRuleToModel(rule)
	if err != nil {
		return err
	}
	return r.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns: []clause.Column{{Name: "name"}},
		DoUpdates: clause.AssignmentColumns([]string{
			"enabled", "priority", "path_prefix", "request_lane",
			"match", "targets", "version", "updated_at",
		}),
	}).Create(m).Error
}

// InsertIfAbsent 以 name 为冲突 key 插入；已存在则 OnConflict DoNothing，
// 一字不覆盖。基线 ensure 走这条，靠 DB 层原子性消除 FindByName 预判的 TOCTOU
// 窗口，并保证人工编辑/并发插入的同名规则永不被基线冲掉。
func (r *GatewayRuleRepo) InsertIfAbsent(ctx context.Context, rule *domain.GatewayRule) error {
	m, err := gatewayRuleToModel(rule)
	if err != nil {
		return err
	}
	return r.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "name"}},
		DoNothing: true,
	}).Create(m).Error
}

func (r *GatewayRuleRepo) FindByName(ctx context.Context, name string) (*domain.GatewayRule, error) {
	var m GatewayRuleModel
	result := r.db.WithContext(ctx).First(&m, "name = ?", name)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrGatewayRuleNotFound
		}
		return nil, result.Error
	}
	return modelToGatewayRule(&m)
}

func (r *GatewayRuleRepo) FindAll(ctx context.Context) ([]*domain.GatewayRule, error) {
	var models []GatewayRuleModel
	if err := r.db.WithContext(ctx).Order("priority desc, name").Find(&models).Error; err != nil {
		return nil, err
	}
	rules := make([]*domain.GatewayRule, 0, len(models))
	for i := range models {
		rule, err := modelToGatewayRule(&models[i])
		if err != nil {
			return nil, err
		}
		rules = append(rules, rule)
	}
	return rules, nil
}

func (r *GatewayRuleRepo) Delete(ctx context.Context, name string) error {
	result := r.db.WithContext(ctx).Delete(&GatewayRuleModel{}, "name = ?", name)
	if result.Error != nil {
		return result.Error
	}
	if result.RowsAffected == 0 {
		return domain.ErrGatewayRuleNotFound
	}
	return nil
}

func gatewayRuleToModel(rule *domain.GatewayRule) (*GatewayRuleModel, error) {
	matchJSON, err := json.Marshal(rule.Match)
	if err != nil {
		return nil, fmt.Errorf("marshal match: %w", err)
	}
	targetsJSON, err := json.Marshal(rule.Targets)
	if err != nil {
		return nil, fmt.Errorf("marshal targets: %w", err)
	}
	return &GatewayRuleModel{
		Name:        rule.Name,
		Enabled:     rule.Enabled,
		Priority:    rule.Priority,
		PathPrefix:  rule.PathPrefix,
		RequestLane: rule.RequestLane,
		Match:       string(matchJSON),
		Targets:     string(targetsJSON),
		Version:     rule.Version,
		CreatedAt:   rule.CreatedAt,
		UpdatedAt:   rule.UpdatedAt,
	}, nil
}

func modelToGatewayRule(m *GatewayRuleModel) (*domain.GatewayRule, error) {
	var match domain.GatewayMatch
	if m.Match != "" {
		if err := json.Unmarshal([]byte(m.Match), &match); err != nil {
			return nil, fmt.Errorf("unmarshal match for rule %q: %w", m.Name, err)
		}
	}
	targets := []domain.GatewayTarget{}
	if m.Targets != "" {
		if err := json.Unmarshal([]byte(m.Targets), &targets); err != nil {
			return nil, fmt.Errorf("unmarshal targets for rule %q: %w", m.Name, err)
		}
	}
	return &domain.GatewayRule{
		Name:        m.Name,
		Enabled:     m.Enabled,
		Priority:    m.Priority,
		PathPrefix:  m.PathPrefix,
		RequestLane: m.RequestLane,
		Match:       match,
		Targets:     targets,
		Version:     m.Version,
		CreatedAt:   m.CreatedAt,
		UpdatedAt:   m.UpdatedAt,
	}, nil
}
