package repository

import (
	"context"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

var _ port.DynamicConfigRepository = (*DynamicConfigRepo)(nil)

type DynamicConfigRepo struct {
	db *gorm.DB
}

func NewDynamicConfigRepo(db *gorm.DB) *DynamicConfigRepo {
	return &DynamicConfigRepo{db: db}
}

func (r *DynamicConfigRepo) Upsert(ctx context.Context, config *domain.DynamicConfig) error {
	m := DynamicConfigModel{
		Key:       config.Key,
		Lane:      config.Lane,
		Value:     config.Value,
		UpdatedAt: config.UpdatedAt,
	}
	return r.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "key"}, {Name: "lane"}},
		DoUpdates: clause.AssignmentColumns([]string{"value", "updated_at"}),
	}).Create(&m).Error
}

func (r *DynamicConfigRepo) FindByKeyAndLane(ctx context.Context, key, lane string) (*domain.DynamicConfig, error) {
	var m DynamicConfigModel
	result := r.db.WithContext(ctx).First(&m, "key = ? AND lane = ?", key, lane)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrDynamicConfigNotFound
		}
		return nil, result.Error
	}
	return modelToDynamicConfig(&m), nil
}

func (r *DynamicConfigRepo) FindByLane(ctx context.Context, lane string) ([]*domain.DynamicConfig, error) {
	var models []DynamicConfigModel
	if err := r.db.WithContext(ctx).Where("lane = ?", lane).Find(&models).Error; err != nil {
		return nil, err
	}
	return modelsToDynamicConfigs(models), nil
}

func (r *DynamicConfigRepo) FindAll(ctx context.Context) ([]*domain.DynamicConfig, error) {
	var models []DynamicConfigModel
	if err := r.db.WithContext(ctx).Order("key, lane").Find(&models).Error; err != nil {
		return nil, err
	}
	return modelsToDynamicConfigs(models), nil
}

func (r *DynamicConfigRepo) DeleteByKeyAndLane(ctx context.Context, key, lane string) error {
	result := r.db.WithContext(ctx).Delete(&DynamicConfigModel{}, "key = ? AND lane = ?", key, lane)
	if result.RowsAffected == 0 {
		return domain.ErrDynamicConfigNotFound
	}
	return result.Error
}

func (r *DynamicConfigRepo) DeleteByKey(ctx context.Context, key string) error {
	result := r.db.WithContext(ctx).Delete(&DynamicConfigModel{}, "key = ?", key)
	if result.RowsAffected == 0 {
		return domain.ErrDynamicConfigNotFound
	}
	return result.Error
}

func modelToDynamicConfig(m *DynamicConfigModel) *domain.DynamicConfig {
	return &domain.DynamicConfig{
		Key:       m.Key,
		Lane:      m.Lane,
		Value:     m.Value,
		UpdatedAt: m.UpdatedAt,
	}
}

func modelsToDynamicConfigs(models []DynamicConfigModel) []*domain.DynamicConfig {
	configs := make([]*domain.DynamicConfig, 0, len(models))
	for i := range models {
		configs = append(configs, modelToDynamicConfig(&models[i]))
	}
	return configs
}
