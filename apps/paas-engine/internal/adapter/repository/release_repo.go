package repository

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

var _ port.ReleaseRepository = (*ReleaseRepo)(nil)

type ReleaseRepo struct {
	db *gorm.DB
}

func NewReleaseRepo(db *gorm.DB) *ReleaseRepo {
	return &ReleaseRepo{db: db}
}

func (r *ReleaseRepo) Save(ctx context.Context, release *domain.Release) error {
	m, err := releaseToModel(release)
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

func (r *ReleaseRepo) FindByID(ctx context.Context, id string) (*domain.Release, error) {
	var m ReleaseModel
	result := r.db.WithContext(ctx).First(&m, "id = ?", id)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrReleaseNotFound
		}
		return nil, result.Error
	}
	return modelToRelease(&m)
}

func (r *ReleaseRepo) FindByAppAndLane(ctx context.Context, appName, lane string) (*domain.Release, error) {
	var m ReleaseModel
	result := r.db.WithContext(ctx).Where("app_name = ? AND lane = ?", appName, lane).First(&m)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrReleaseNotFound
		}
		return nil, result.Error
	}
	return modelToRelease(&m)
}

func (r *ReleaseRepo) FindAll(ctx context.Context, appName, lane string) ([]*domain.Release, error) {
	query := r.db.WithContext(ctx).Model(&ReleaseModel{})
	if appName != "" {
		query = query.Where("app_name = ?", appName)
	}
	if lane != "" {
		query = query.Where("lane = ?", lane)
	}
	var models []ReleaseModel
	if err := query.Find(&models).Error; err != nil {
		return nil, err
	}
	releases := make([]*domain.Release, 0, len(models))
	for i := range models {
		rel, err := modelToRelease(&models[i])
		if err != nil {
			return nil, err
		}
		releases = append(releases, rel)
	}
	return releases, nil
}

func (r *ReleaseRepo) Update(ctx context.Context, release *domain.Release) error {
	m, err := releaseToModel(release)
	if err != nil {
		return err
	}
	return r.db.WithContext(ctx).Save(m).Error
}

func (r *ReleaseRepo) Delete(ctx context.Context, id string) error {
	return r.db.WithContext(ctx).Delete(&ReleaseModel{}, "id = ?", id).Error
}

func (r *ReleaseRepo) FindByLane(ctx context.Context, lane string) ([]*domain.Release, error) {
	var models []ReleaseModel
	if err := r.db.WithContext(ctx).Where("lane = ?", lane).Find(&models).Error; err != nil {
		return nil, err
	}
	releases := make([]*domain.Release, 0, len(models))
	for i := range models {
		rel, err := modelToRelease(&models[i])
		if err != nil {
			return nil, err
		}
		releases = append(releases, rel)
	}
	return releases, nil
}

func releaseToModel(r *domain.Release) (*ReleaseModel, error) {
	envsJSON, err := json.Marshal(r.Envs)
	if err != nil {
		return nil, err
	}
	return &ReleaseModel{
		ID:         r.ID,
		AppName:    r.AppName,
		Lane:       r.Lane,
		Image:      r.Image,
		Replicas:   r.Replicas,
		Envs:       string(envsJSON),
		Version:    r.Version,
		Status:     string(r.Status),
		DeployName: r.DeployName,
		CreatedAt:  r.CreatedAt,
		UpdatedAt:  r.UpdatedAt,
	}, nil
}

func modelToRelease(m *ReleaseModel) (*domain.Release, error) {
	var envs map[string]string
	if m.Envs != "" {
		if err := json.Unmarshal([]byte(m.Envs), &envs); err != nil {
			return nil, err
		}
	}
	return &domain.Release{
		ID:         m.ID,
		AppName:    m.AppName,
		Lane:       m.Lane,
		Image:      m.Image,
		Replicas:   m.Replicas,
		Envs:       envs,
		Version:    m.Version,
		Status:     domain.ReleaseStatus(m.Status),
		DeployName: m.DeployName,
		CreatedAt:  m.CreatedAt,
		UpdatedAt:  m.UpdatedAt,
	}, nil
}
