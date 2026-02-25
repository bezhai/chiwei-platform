package service

import (
	"context"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type AppService struct {
	appRepo     port.AppRepository
	releaseRepo port.ReleaseRepository
}

func NewAppService(appRepo port.AppRepository, releaseRepo port.ReleaseRepository) *AppService {
	return &AppService{appRepo: appRepo, releaseRepo: releaseRepo}
}

type CreateAppRequest struct {
	Name           string            `json:"name"`
	Description    string            `json:"description"`
	Image          string            `json:"image"`
	Port           int               `json:"port"`
	ServiceAccount string            `json:"service_account"`
	EnvFromSecrets []string          `json:"env_from_secrets"`
	Envs           map[string]string `json:"envs"`
	ContextDir     string            `json:"context_dir"`
}

func (s *AppService) CreateApp(ctx context.Context, req CreateAppRequest) (*domain.App, error) {
	if err := domain.ValidateK8sName(req.Name); err != nil {
		return nil, err
	}
	if req.Port <= 0 {
		return nil, domain.ErrInvalidInput
	}
	if err := domain.ValidateContextDir(req.ContextDir); err != nil {
		return nil, err
	}
	now := time.Now()
	app := &domain.App{
		Name:           req.Name,
		Description:    req.Description,
		Image:          req.Image,
		Port:           req.Port,
		ServiceAccount: req.ServiceAccount,
		EnvFromSecrets: req.EnvFromSecrets,
		Envs:           req.Envs,
		ContextDir:     req.ContextDir,
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := s.appRepo.Save(ctx, app); err != nil {
		return nil, err
	}
	return app, nil
}

func (s *AppService) GetApp(ctx context.Context, name string) (*domain.App, error) {
	return s.appRepo.FindByName(ctx, name)
}

func (s *AppService) ListApps(ctx context.Context) ([]*domain.App, error) {
	return s.appRepo.FindAll(ctx)
}

type UpdateAppRequest struct {
	Description    string            `json:"description"`
	Image          string            `json:"image"`
	Port           int               `json:"port"`
	ServiceAccount string            `json:"service_account"`
	EnvFromSecrets []string          `json:"env_from_secrets"`
	Envs           map[string]string `json:"envs"`
	ContextDir     string            `json:"context_dir"`
}

func (s *AppService) UpdateApp(ctx context.Context, name string, req UpdateAppRequest) (*domain.App, error) {
	app, err := s.appRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}
	if err := domain.ValidateContextDir(req.ContextDir); err != nil {
		return nil, err
	}
	app.Description = req.Description
	app.Image = req.Image
	if req.Port > 0 {
		app.Port = req.Port
	}
	app.ServiceAccount = req.ServiceAccount
	app.EnvFromSecrets = req.EnvFromSecrets
	app.Envs = req.Envs
	app.ContextDir = req.ContextDir
	app.UpdatedAt = time.Now()
	if err := s.appRepo.Update(ctx, app); err != nil {
		return nil, err
	}
	return app, nil
}

func (s *AppService) DeleteApp(ctx context.Context, name string) error {
	if _, err := s.appRepo.FindByName(ctx, name); err != nil {
		return err
	}
	// 检查是否还有关联的 Release
	releases, err := s.releaseRepo.FindAll(ctx, name, "")
	if err != nil {
		return err
	}
	if len(releases) > 0 {
		return domain.ErrCannotDelete
	}
	return s.appRepo.Delete(ctx, name)
}

