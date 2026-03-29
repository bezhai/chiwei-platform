package service

import (
	"context"
	"fmt"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type AppService struct {
	appRepo          port.AppRepository
	imageRepoRepo    port.ImageRepoRepository
	releaseRepo      port.ReleaseRepository
	configBundleRepo port.ConfigBundleRepository
}

func NewAppService(appRepo port.AppRepository, imageRepoRepo port.ImageRepoRepository, releaseRepo port.ReleaseRepository, configBundleRepo port.ConfigBundleRepository) *AppService {
	return &AppService{appRepo: appRepo, imageRepoRepo: imageRepoRepo, releaseRepo: releaseRepo, configBundleRepo: configBundleRepo}
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
	ConfigBundles     []string          `json:"config_bundles"`
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

	if len(req.ConfigBundles) > 0 {
		if err := s.validateConfigBundles(ctx, req.ConfigBundles); err != nil {
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
		ConfigBundles:     req.ConfigBundles,
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
	if err := ApplyField(fields, "config_bundles", &app.ConfigBundles); err != nil {
		return nil, domain.ErrInvalidInput
	}
	if _, ok := fields["config_bundles"]; ok && len(app.ConfigBundles) > 0 {
		if err := s.validateConfigBundles(ctx, app.ConfigBundles); err != nil {
			return nil, err
		}
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

func (s *AppService) validateConfigBundles(ctx context.Context, bundleNames []string) error {
	if s.configBundleRepo == nil {
		return nil
	}
	bundles, err := s.configBundleRepo.FindByNames(ctx, bundleNames)
	if err != nil {
		return err
	}
	if len(bundles) != len(bundleNames) {
		found := make(map[string]bool)
		for _, b := range bundles {
			found[b.Name] = true
		}
		for _, name := range bundleNames {
			if !found[name] {
				return fmt.Errorf("%w: config bundle %q not found", domain.ErrInvalidInput, name)
			}
		}
	}
	// Check key conflicts across bundles
	seen := make(map[string]string) // key name → bundle name
	for _, bundle := range bundles {
		for key := range bundle.Keys {
			if other, ok := seen[key]; ok {
				return fmt.Errorf("%w: key %q defined in both %q and %q", domain.ErrInvalidInput, key, other, bundle.Name)
			}
			seen[key] = bundle.Name
		}
	}
	return nil
}
