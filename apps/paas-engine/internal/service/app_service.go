package service

import (
	"context"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type AppService struct {
	appRepo       port.AppRepository
	imageRepoRepo port.ImageRepoRepository
	releaseRepo   port.ReleaseRepository
}

func NewAppService(appRepo port.AppRepository, imageRepoRepo port.ImageRepoRepository, releaseRepo port.ReleaseRepository) *AppService {
	return &AppService{appRepo: appRepo, imageRepoRepo: imageRepoRepo, releaseRepo: releaseRepo}
}

type CreateAppRequest struct {
	Name              string            `json:"name"`
	Description       string            `json:"description"`
	ImageRepoName     string            `json:"image_repo"`
	Port              int               `json:"port"`
	ServiceAccount    string            `json:"service_account"`
	Command           []string          `json:"command"`
	EnvFromSecrets    []string          `json:"env_from_secrets"`
	EnvFromConfigMaps []string          `json:"env_from_config_maps"`
	Envs              map[string]string `json:"envs"`
}

func (s *AppService) CreateApp(ctx context.Context, req CreateAppRequest) (*domain.App, error) {
	if err := domain.ValidateK8sName(req.Name); err != nil {
		return nil, err
	}
	if req.Port < 0 {
		return nil, domain.ErrInvalidInput
	}
	// 校验 ImageRepo 存在
	if req.ImageRepoName != "" {
		if _, err := s.imageRepoRepo.FindByName(ctx, req.ImageRepoName); err != nil {
			return nil, err
		}
	}

	now := time.Now()
	app := &domain.App{
		Name:              req.Name,
		Description:       req.Description,
		ImageRepoName:     req.ImageRepoName,
		Port:              req.Port,
		ServiceAccount:    req.ServiceAccount,
		Command:           req.Command,
		EnvFromSecrets:    req.EnvFromSecrets,
		EnvFromConfigMaps: req.EnvFromConfigMaps,
		Envs:              req.Envs,
		CreatedAt:         now,
		UpdatedAt:         now,
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

func (s *AppService) UpdateApp(ctx context.Context, name string, body []byte) (*domain.App, error) {
	app, err := s.appRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}

	fields, err := ParseFields(body)
	if err != nil {
		return nil, domain.ErrInvalidInput
	}

	// 标量/切片字段：出现则更新
	if err := ApplyField(fields, "description", &app.Description); err != nil {
		return nil, domain.ErrInvalidInput
	}
	if err := ApplyField(fields, "image_repo", &app.ImageRepoName); err != nil {
		return nil, domain.ErrInvalidInput
	}
	if err := ApplyField(fields, "port", &app.Port); err != nil {
		return nil, domain.ErrInvalidInput
	}
	if err := ApplyField(fields, "service_account", &app.ServiceAccount); err != nil {
		return nil, domain.ErrInvalidInput
	}
	if err := ApplyField(fields, "command", &app.Command); err != nil {
		return nil, domain.ErrInvalidInput
	}
	if err := ApplyField(fields, "env_from_secrets", &app.EnvFromSecrets); err != nil {
		return nil, domain.ErrInvalidInput
	}
	if err := ApplyField(fields, "env_from_config_maps", &app.EnvFromConfigMaps); err != nil {
		return nil, domain.ErrInvalidInput
	}

	// Map 字段：按 key 合并
	app.Envs, err = MergeEnvs(app.Envs, fields["envs"])
	if err != nil {
		return nil, domain.ErrInvalidInput
	}

	// 合并后校验 ImageRepo 存在
	if app.ImageRepoName != "" {
		if _, err := s.imageRepoRepo.FindByName(ctx, app.ImageRepoName); err != nil {
			return nil, err
		}
	}

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
