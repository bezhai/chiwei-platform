package service

import (
	"context"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type ImageRepoService struct {
	imageRepoRepo port.ImageRepoRepository
	appRepo       port.AppRepository
}

func NewImageRepoService(imageRepoRepo port.ImageRepoRepository, appRepo port.AppRepository) *ImageRepoService {
	return &ImageRepoService{imageRepoRepo: imageRepoRepo, appRepo: appRepo}
}

type CreateImageRepoRequest struct {
	Name       string `json:"name"`
	Registry   string `json:"registry"`
	GitRepo    string `json:"git_repo"`
	ContextDir string `json:"context_dir"`
}

func (s *ImageRepoService) CreateImageRepo(ctx context.Context, req CreateImageRepoRequest) (*domain.ImageRepo, error) {
	if err := domain.ValidateK8sName(req.Name); err != nil {
		return nil, err
	}
	if req.Registry == "" {
		return nil, domain.ErrInvalidInput
	}
	if err := domain.ValidateGitRepo(req.GitRepo); err != nil {
		return nil, err
	}
	if err := domain.ValidateContextDir(req.ContextDir); err != nil {
		return nil, err
	}

	now := time.Now()
	repo := &domain.ImageRepo{
		Name:       req.Name,
		Registry:   req.Registry,
		GitRepo:    req.GitRepo,
		ContextDir: req.ContextDir,
		CreatedAt:  now,
		UpdatedAt:  now,
	}
	if err := s.imageRepoRepo.Save(ctx, repo); err != nil {
		return nil, err
	}
	return repo, nil
}

func (s *ImageRepoService) GetImageRepo(ctx context.Context, name string) (*domain.ImageRepo, error) {
	return s.imageRepoRepo.FindByName(ctx, name)
}

func (s *ImageRepoService) ListImageRepos(ctx context.Context) ([]*domain.ImageRepo, error) {
	return s.imageRepoRepo.FindAll(ctx)
}

type UpdateImageRepoRequest struct {
	Registry   string `json:"registry"`
	GitRepo    string `json:"git_repo"`
	ContextDir string `json:"context_dir"`
}

func (s *ImageRepoService) UpdateImageRepo(ctx context.Context, name string, req UpdateImageRepoRequest) (*domain.ImageRepo, error) {
	repo, err := s.imageRepoRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}
	if req.Registry == "" {
		return nil, domain.ErrInvalidInput
	}
	if err := domain.ValidateGitRepo(req.GitRepo); err != nil {
		return nil, err
	}
	if err := domain.ValidateContextDir(req.ContextDir); err != nil {
		return nil, err
	}

	repo.Registry = req.Registry
	repo.GitRepo = req.GitRepo
	repo.ContextDir = req.ContextDir
	repo.UpdatedAt = time.Now()
	if err := s.imageRepoRepo.Update(ctx, repo); err != nil {
		return nil, err
	}
	return repo, nil
}

func (s *ImageRepoService) DeleteImageRepo(ctx context.Context, name string) error {
	if _, err := s.imageRepoRepo.FindByName(ctx, name); err != nil {
		return err
	}
	// 检查是否有 App 引用此 ImageRepo
	apps, err := s.appRepo.FindAll(ctx)
	if err != nil {
		return err
	}
	for _, app := range apps {
		if app.ImageRepoName == name {
			return domain.ErrCannotDelete
		}
	}
	return s.imageRepoRepo.Delete(ctx, name)
}
