package repository

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

var _ port.AppRepository = (*AppRepo)(nil)

type AppRepo struct {
	db *gorm.DB
}

func NewAppRepo(db *gorm.DB) *AppRepo {
	return &AppRepo{db: db}
}

func (r *AppRepo) Save(ctx context.Context, app *domain.App) error {
	m, err := appToModel(app)
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

func (r *AppRepo) FindByName(ctx context.Context, name string) (*domain.App, error) {
	var m AppModel
	result := r.db.WithContext(ctx).First(&m, "name = ?", name)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrAppNotFound
		}
		return nil, result.Error
	}
	return modelToApp(&m)
}

func (r *AppRepo) FindAll(ctx context.Context) ([]*domain.App, error) {
	var models []AppModel
	if err := r.db.WithContext(ctx).Find(&models).Error; err != nil {
		return nil, err
	}
	apps := make([]*domain.App, 0, len(models))
	for i := range models {
		a, err := modelToApp(&models[i])
		if err != nil {
			return nil, err
		}
		apps = append(apps, a)
	}
	return apps, nil
}

func (r *AppRepo) Update(ctx context.Context, app *domain.App) error {
	m, err := appToModel(app)
	if err != nil {
		return err
	}
	return r.db.WithContext(ctx).Save(m).Error
}

func (r *AppRepo) Delete(ctx context.Context, name string) error {
	return r.db.WithContext(ctx).Delete(&AppModel{}, "name = ?", name).Error
}

func appToModel(a *domain.App) (*AppModel, error) {
	envsJSON, err := json.Marshal(a.Envs)
	if err != nil {
		return nil, err
	}
	envFromSecretsJSON, err := json.Marshal(a.EnvFromSecrets)
	if err != nil {
		return nil, err
	}
	commandJSON, err := json.Marshal(a.Command)
	if err != nil {
		return nil, err
	}
	envFromConfigMapsJSON, err := json.Marshal(a.EnvFromConfigMaps)
	if err != nil {
		return nil, err
	}
	return &AppModel{
		Name:              a.Name,
		Description:       a.Description,
		ImageRepoName:     a.ImageRepoName,
		Port:              a.Port,
		ServiceAccount:    a.ServiceAccount,
		Command:           string(commandJSON),
		EnvFromSecrets:    string(envFromSecretsJSON),
		EnvFromConfigMaps: string(envFromConfigMapsJSON),
		Envs:              string(envsJSON),
		CreatedAt:         a.CreatedAt,
		UpdatedAt:         a.UpdatedAt,
	}, nil
}

func modelToApp(m *AppModel) (*domain.App, error) {
	var envs map[string]string
	if m.Envs != "" {
		if err := json.Unmarshal([]byte(m.Envs), &envs); err != nil {
			return nil, err
		}
	}
	var envFromSecrets []string
	if m.EnvFromSecrets != "" {
		if err := json.Unmarshal([]byte(m.EnvFromSecrets), &envFromSecrets); err != nil {
			return nil, err
		}
	}
	var command []string
	if m.Command != "" {
		if err := json.Unmarshal([]byte(m.Command), &command); err != nil {
			return nil, err
		}
	}
	var envFromConfigMaps []string
	if m.EnvFromConfigMaps != "" {
		if err := json.Unmarshal([]byte(m.EnvFromConfigMaps), &envFromConfigMaps); err != nil {
			return nil, err
		}
	}
	return &domain.App{
		Name:              m.Name,
		Description:       m.Description,
		ImageRepoName:     m.ImageRepoName,
		Port:              m.Port,
		ServiceAccount:    m.ServiceAccount,
		Command:           command,
		EnvFromSecrets:    envFromSecrets,
		EnvFromConfigMaps: envFromConfigMaps,
		Envs:              envs,
		CreatedAt:         m.CreatedAt,
		UpdatedAt:         m.UpdatedAt,
	}, nil
}
