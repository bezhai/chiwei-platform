package port

import (
	"context"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// Deployer 负责将 Release 翻译为 K8s Deployment + Service 并下发。
type Deployer interface {
	Deploy(ctx context.Context, release *domain.Release, app *domain.App) error
	Delete(ctx context.Context, release *domain.Release) error
}
