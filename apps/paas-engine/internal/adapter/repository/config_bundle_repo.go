package repository

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

var _ port.ConfigBundleRepository = (*ConfigBundleRepo)(nil)

type ConfigBundleRepo struct {
	db *gorm.DB
}

func NewConfigBundleRepo(db *gorm.DB) *ConfigBundleRepo {
	return &ConfigBundleRepo{db: db}
}

func (r *ConfigBundleRepo) Save(ctx context.Context, bundle *domain.ConfigBundle) error {
	m, err := bundleToModel(bundle)
	if err != nil {
		return err
	}
	result := r.db.WithContext(ctx).Create(m)
	if result.Error != nil {
		if isUniqueConstraintError(result.Error) {
			return domain.ErrAlreadyExists
		}
		return result.Error
	}
	return nil
}

func (r *ConfigBundleRepo) FindByName(ctx context.Context, name string) (*domain.ConfigBundle, error) {
	var m ConfigBundleModel
	result := r.db.WithContext(ctx).First(&m, "name = ?", name)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrConfigBundleNotFound
		}
		return nil, result.Error
	}
	return modelToBundle(&m)
}

func (r *ConfigBundleRepo) FindAll(ctx context.Context) ([]*domain.ConfigBundle, error) {
	var models []ConfigBundleModel
	if err := r.db.WithContext(ctx).Find(&models).Error; err != nil {
		return nil, err
	}
	bundles := make([]*domain.ConfigBundle, 0, len(models))
	for i := range models {
		b, err := modelToBundle(&models[i])
		if err != nil {
			return nil, err
		}
		bundles = append(bundles, b)
	}
	return bundles, nil
}

func (r *ConfigBundleRepo) FindByNames(ctx context.Context, names []string) ([]*domain.ConfigBundle, error) {
	var models []ConfigBundleModel
	if err := r.db.WithContext(ctx).Where("name IN ?", names).Find(&models).Error; err != nil {
		return nil, err
	}
	bundles := make([]*domain.ConfigBundle, 0, len(models))
	for i := range models {
		b, err := modelToBundle(&models[i])
		if err != nil {
			return nil, err
		}
		bundles = append(bundles, b)
	}
	return bundles, nil
}

func (r *ConfigBundleRepo) Update(ctx context.Context, bundle *domain.ConfigBundle) error {
	m, err := bundleToModel(bundle)
	if err != nil {
		return err
	}
	return r.db.WithContext(ctx).Save(m).Error
}

func (r *ConfigBundleRepo) Delete(ctx context.Context, name string) error {
	return r.db.WithContext(ctx).Delete(&ConfigBundleModel{}, "name = ?", name).Error
}

func bundleToModel(b *domain.ConfigBundle) (*ConfigBundleModel, error) {
	keysJSON, err := json.Marshal(b.Keys)
	if err != nil {
		return nil, err
	}
	laneOverridesJSON, err := json.Marshal(b.LaneOverrides)
	if err != nil {
		return nil, err
	}
	return &ConfigBundleModel{
		Name:          b.Name,
		Description:   b.Description,
		Keys:          string(keysJSON),
		LaneOverrides: string(laneOverridesJSON),
		CreatedAt:     b.CreatedAt,
		UpdatedAt:     b.UpdatedAt,
	}, nil
}

func modelToBundle(m *ConfigBundleModel) (*domain.ConfigBundle, error) {
	var keys map[string]string
	if m.Keys != "" {
		if err := json.Unmarshal([]byte(m.Keys), &keys); err != nil {
			return nil, err
		}
	}
	var laneOverrides map[string]map[string]string
	if m.LaneOverrides != "" {
		if err := json.Unmarshal([]byte(m.LaneOverrides), &laneOverrides); err != nil {
			return nil, err
		}
	}
	return &domain.ConfigBundle{
		Name:          m.Name,
		Description:   m.Description,
		Keys:          keys,
		LaneOverrides: laneOverrides,
		CreatedAt:     m.CreatedAt,
		UpdatedAt:     m.UpdatedAt,
	}, nil
}
