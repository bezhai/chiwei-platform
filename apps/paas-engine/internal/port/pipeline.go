package port

import (
	"context"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// TestStatusCallback 在测试 Job 状态变更时被调用。
type TestStatusCallback func(jobRunID string, status domain.PipelineRunStatus, log string)

// TestSubmission 封装提交给 TestExecutor 的测试参数。
type TestSubmission struct {
	JobRunID string
	GitRepo  string
	GitRef   string
	Runtime  string // python / bun / go
	Command  string // 测试命令
	Envs     map[string]string
}

// TestExecutor 负责驱动测试 K8s Job 的生命周期。
type TestExecutor interface {
	Submit(ctx context.Context, sub *TestSubmission) (jobName string, err error)
	Cancel(ctx context.Context, jobName string) error
	Watch(ctx context.Context, callback TestStatusCallback) error
	GetLogs(ctx context.Context, jobRunID string) (string, error)
}

// CIConfigRepository 管理 CI 配置的持久化。
type CIConfigRepository interface {
	Save(ctx context.Context, cfg *domain.CIConfig) error
	FindByID(ctx context.Context, id string) (*domain.CIConfig, error)
	FindByLane(ctx context.Context, lane string) (*domain.CIConfig, error)
	FindByBranch(ctx context.Context, branch string) (*domain.CIConfig, error)
	FindActive(ctx context.Context) ([]*domain.CIConfig, error)
	Update(ctx context.Context, cfg *domain.CIConfig) error
	Delete(ctx context.Context, id string) error
}

// PipelineRunRepository 管理 pipeline 执行记录的持久化。
type PipelineRunRepository interface {
	Save(ctx context.Context, run *domain.PipelineRun) error
	FindByID(ctx context.Context, id string) (*domain.PipelineRun, error)
	FindByLane(ctx context.Context, lane string, limit int) ([]*domain.PipelineRun, error)
	ExistsByCommitSHA(ctx context.Context, sha string) (bool, error)
	Update(ctx context.Context, run *domain.PipelineRun) error

	SaveStage(ctx context.Context, stage *domain.StageRun) error
	FindStagesByRunID(ctx context.Context, runID string) ([]domain.StageRun, error)
	UpdateStage(ctx context.Context, stage *domain.StageRun) error

	SaveJob(ctx context.Context, job *domain.JobRun) error
	FindJobsByStageID(ctx context.Context, stageID string) ([]domain.JobRun, error)
	FindJobByID(ctx context.Context, id string) (*domain.JobRun, error)
	UpdateJob(ctx context.Context, job *domain.JobRun) error
}
