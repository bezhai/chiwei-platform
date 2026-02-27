package repository

import (
	"context"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

var _ port.ImageRepoRepository = (*ImageRepoRepo)(nil)

type ImageRepoRepo struct {
	db *gorm.DB
}

func NewImageRepoRepo(db *gorm.DB) *ImageRepoRepo {
	return &ImageRepoRepo{db: db}
}

func (r *ImageRepoRepo) Save(ctx context.Context, repo *domain.ImageRepo) error {
	m := imageRepoToModel(repo)
	result := r.db.WithContext(ctx).Create(m)
	if result.Error != nil {
		if isUniqueConstraintError(result.Error) {
			return domain.ErrAlreadyExists
		}
		return result.Error
	}
	return nil
}

func (r *ImageRepoRepo) FindByName(ctx context.Context, name string) (*domain.ImageRepo, error) {
	var m ImageRepoModel
	result := r.db.WithContext(ctx).First(&m, "name = ?", name)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrImageRepoNotFound
		}
		return nil, result.Error
	}
	return modelToImageRepo(&m), nil
}

func (r *ImageRepoRepo) FindAll(ctx context.Context) ([]*domain.ImageRepo, error) {
	var models []ImageRepoModel
	if err := r.db.WithContext(ctx).Find(&models).Error; err != nil {
		return nil, err
	}
	repos := make([]*domain.ImageRepo, 0, len(models))
	for i := range models {
		repos = append(repos, modelToImageRepo(&models[i]))
	}
	return repos, nil
}

func (r *ImageRepoRepo) Update(ctx context.Context, repo *domain.ImageRepo) error {
	m := imageRepoToModel(repo)
	return r.db.WithContext(ctx).Save(m).Error
}

func (r *ImageRepoRepo) Delete(ctx context.Context, name string) error {
	return r.db.WithContext(ctx).Delete(&ImageRepoModel{}, "name = ?", name).Error
}

func imageRepoToModel(ir *domain.ImageRepo) *ImageRepoModel {
	return &ImageRepoModel{
		Name:       ir.Name,
		Registry:   ir.Registry,
		GitRepo:    ir.GitRepo,
		ContextDir: ir.ContextDir,
		Dockerfile: ir.Dockerfile,
		CreatedAt:  ir.CreatedAt,
		UpdatedAt:  ir.UpdatedAt,
	}
}

func modelToImageRepo(m *ImageRepoModel) *domain.ImageRepo {
	return &domain.ImageRepo{
		Name:       m.Name,
		Registry:   m.Registry,
		GitRepo:    m.GitRepo,
		ContextDir: m.ContextDir,
		Dockerfile: m.Dockerfile,
		CreatedAt:  m.CreatedAt,
		UpdatedAt:  m.UpdatedAt,
	}
}
