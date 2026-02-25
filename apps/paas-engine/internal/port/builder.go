package port

import (
	"context"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// BuildStatusCallback 在 Job 状态变更时被调用。
type BuildStatusCallback func(buildID string, status domain.BuildStatus, log string)

// LogQuerier 查询历史构建日志（如 Loki）。
type LogQuerier interface {
	QueryBuildLogs(ctx context.Context, namespace, buildID string, start, end time.Time) (string, error)
	QueryAppLogs(ctx context.Context, namespace, appName, lane string, start, end time.Time, limit int) (string, error)
}

// BuildExecutor 负责驱动 Kaniko Job 的生命周期。
type BuildExecutor interface {
	// Submit 创建 Kaniko Job 并返回 Job 名称。
	Submit(ctx context.Context, build *domain.Build) (jobName string, err error)
	// Cancel 删除对应 Job。
	Cancel(ctx context.Context, jobName string) error
	// Watch 启动 Informer 监听，状态变更时调用 callback。
	Watch(ctx context.Context, callback BuildStatusCallback) error
	// GetLogs 获取构建 Pod 的容器日志。
	GetLogs(ctx context.Context, buildID string) (string, error)
}
