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

type LaneRepository interface {
	Save(ctx context.Context, lane *domain.Lane) error
	FindByName(ctx context.Context, name string) (*domain.Lane, error)
	FindAll(ctx context.Context) ([]*domain.Lane, error)
	Delete(ctx context.Context, name string) error
}

type BuildRepository interface {
	Save(ctx context.Context, build *domain.Build) error
	FindByID(ctx context.Context, id string) (*domain.Build, error)
	FindByImageRepo(ctx context.Context, imageRepoName string) ([]*domain.Build, error)
	FindLatestSuccessful(ctx context.Context, imageRepoName string) (*domain.Build, error)
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
	FindByLane(ctx context.Context, lane string) ([]*domain.Release, error)
}
