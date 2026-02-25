package repository

import (
	"context"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

var _ port.BuildRepository = (*BuildRepo)(nil)

type BuildRepo struct {
	db *gorm.DB
}

func NewBuildRepo(db *gorm.DB) *BuildRepo {
	return &BuildRepo{db: db}
}

func (r *BuildRepo) Save(ctx context.Context, build *domain.Build) error {
	m := buildToModel(build)
	return r.db.WithContext(ctx).Create(m).Error
}

func (r *BuildRepo) FindByID(ctx context.Context, id string) (*domain.Build, error) {
	var m BuildModel
	result := r.db.WithContext(ctx).First(&m, "id = ?", id)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrBuildNotFound
		}
		return nil, result.Error
	}
	return modelToBuild(&m), nil
}

func (r *BuildRepo) FindByApp(ctx context.Context, appName string) ([]*domain.Build, error) {
	var models []BuildModel
	if err := r.db.WithContext(ctx).Where("app_name = ?", appName).Order("created_at desc").Find(&models).Error; err != nil {
		return nil, err
	}
	builds := make([]*domain.Build, 0, len(models))
	for i := range models {
		builds = append(builds, modelToBuild(&models[i]))
	}
	return builds, nil
}

func (r *BuildRepo) Update(ctx context.Context, build *domain.Build) error {
	m := buildToModel(build)
	return r.db.WithContext(ctx).Save(m).Error
}

func buildToModel(b *domain.Build) *BuildModel {
	return &BuildModel{
		ID:         b.ID,
		AppName:    b.AppName,
		GitRepo:    b.GitRepo,
		GitRef:     b.GitRef,
		ImageTag:   b.ImageTag,
		ContextDir: b.ContextDir,
		Status:     string(b.Status),
		JobName:    b.JobName,
		Log:        b.Log,
		CreatedAt:  b.CreatedAt,
		UpdatedAt:  b.UpdatedAt,
	}
}

func modelToBuild(m *BuildModel) *domain.Build {
	return &domain.Build{
		ID:         m.ID,
		AppName:    m.AppName,
		GitRepo:    m.GitRepo,
		GitRef:     m.GitRef,
		ImageTag:   m.ImageTag,
		ContextDir: m.ContextDir,
		Status:     domain.BuildStatus(m.Status),
		JobName:    m.JobName,
		Log:        m.Log,
		CreatedAt:  m.CreatedAt,
		UpdatedAt:  m.UpdatedAt,
	}
}
