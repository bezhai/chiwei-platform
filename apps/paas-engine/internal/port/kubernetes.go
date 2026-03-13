package port

import (
	"context"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// Deployer 负责将 Release 翻译为 K8s Deployment + Service 并下发。
type Deployer interface {
	Deploy(ctx context.Context, release *domain.Release, app *domain.App) error
	Delete(ctx context.Context, release *domain.Release, hasOtherReleases bool) error
	// GetDeploymentStatus 查询指定 Deployment 的运行时状态（副本数 + Pod 列表）。
	GetDeploymentStatus(ctx context.Context, name string) (*domain.DeploymentStatus, error)
	// ListManagedResources 列出 namespace 中所有带 app label 的 Deployment 和 Service。
	ListManagedResources(ctx context.Context) ([]ManagedResource, error)
	// DeleteResource 删除指定的 K8s 资源（Deployment 或 Service）。
	DeleteResource(ctx context.Context, kind, name string) error
}

// ManagedResource 表示一个由 paas-engine 管理的 K8s 资源。
type ManagedResource struct {
	Kind    string `json:"kind"`    // "Deployment" or "Service"
	Name    string `json:"name"`    // 资源名称
	AppName string `json:"app"`     // app label 值
	Lane    string `json:"lane"`    // lane label 值（base service 无 lane）
}
