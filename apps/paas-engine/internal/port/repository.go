package port

import (
	"context"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

type AppRepository interface {
	Save(ctx context.Context, app *domain.App) error
	FindByName(ctx context.Context, name string) (*domain.App, error)
	FindAll(ctx context.Context) ([]*domain.App, error)
	Update(ctx context.Context, app *domain.App) error
	Delete(ctx context.Context, name string) error
}

type BuildRepository interface {
	Save(ctx context.Context, build *domain.Build) error
	FindByID(ctx context.Context, id string) (*domain.Build, error)
	FindByImageRepo(ctx context.Context, imageRepoName string) ([]*domain.Build, error)
	FindLatestSuccessful(ctx context.Context, imageRepoName string) (*domain.Build, error)
	FindLatestVersioned(ctx context.Context, imageRepoName string) (*domain.Build, error)
	FindByImageTag(ctx context.Context, imageTag string) (*domain.Build, error)
	Update(ctx context.Context, build *domain.Build) error
}

type ImageRepoRepository interface {
	Save(ctx context.Context, repo *domain.ImageRepo) error
	FindByName(ctx context.Context, name string) (*domain.ImageRepo, error)
	FindAll(ctx context.Context) ([]*domain.ImageRepo, error)
	Update(ctx context.Context, repo *domain.ImageRepo) error
	Delete(ctx context.Context, name string) error
}

type ReleaseRepository interface {
	Save(ctx context.Context, release *domain.Release) error
	FindByID(ctx context.Context, id string) (*domain.Release, error)
	FindByAppAndLane(ctx context.Context, appName, lane string) (*domain.Release, error)
	FindAll(ctx context.Context, appName, lane string) ([]*domain.Release, error)
	Update(ctx context.Context, release *domain.Release) error
	Delete(ctx context.Context, id string) error
}

type ConfigBundleRepository interface {
	Save(ctx context.Context, bundle *domain.ConfigBundle) error
	FindByName(ctx context.Context, name string) (*domain.ConfigBundle, error)
	FindAll(ctx context.Context) ([]*domain.ConfigBundle, error)
	FindByNames(ctx context.Context, names []string) ([]*domain.ConfigBundle, error)
	Update(ctx context.Context, bundle *domain.ConfigBundle) error
	Delete(ctx context.Context, name string) error
}

type DynamicConfigRepository interface {
	Upsert(ctx context.Context, config *domain.DynamicConfig) error
	FindByKeyAndLane(ctx context.Context, key, lane string) (*domain.DynamicConfig, error)
	FindByLane(ctx context.Context, lane string) ([]*domain.DynamicConfig, error)
	FindAll(ctx context.Context) ([]*domain.DynamicConfig, error)
	DeleteByKeyAndLane(ctx context.Context, key, lane string) error
	DeleteByKey(ctx context.Context, key string) error
}
