package domain

import "time"

// PipelineRunStatus 是 PipelineRun / StageRun / JobRun 共用的状态枚举。
// 状态流转：Pending → Running → (Succeeded | Failed | Cancelled)
type PipelineRunStatus string

const (
	PipelineRunPending   PipelineRunStatus = "pending"
	PipelineRunRunning   PipelineRunStatus = "running"
	PipelineRunSucceeded PipelineRunStatus = "succeeded"
	PipelineRunFailed    PipelineRunStatus = "failed"
	PipelineRunCancelled PipelineRunStatus = "cancelled"
)

func (s PipelineRunStatus) IsTerminal() bool {
	return s == PipelineRunSucceeded || s == PipelineRunFailed || s == PipelineRunCancelled
}

// StageType 表示 pipeline 阶段类型。
type StageType string

const (
	StageUnitTest StageType = "unit-test"
	StageBuild    StageType = "build"
	StageDeploy   StageType = "deploy"
	StageE2E      StageType = "e2e"
)

// CIConfig 注册一个 CI 泳道（make ci-init 创建）。
type CIConfig struct {
	ID        string    `json:"id"`
	Lane      string    `json:"lane"`     // 泳道名，如 "feat-auth"
	Branch    string    `json:"branch"`   // 监听的分支，如 "feat/auth-rework"
	Services  []string  `json:"services"` // 要构建/部署/测试的服务列表
	Status    string    `json:"status"`   // "active" / "archived"
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// PipelineRun 代表一次 pipeline 执行。
type PipelineRun struct {
	ID         string            `json:"id"`
	CIConfigID string            `json:"ci_config_id,omitempty"` // 关联的 CIConfig（main 分支时为空）
	GitRef     string            `json:"git_ref"`
	CommitSHA  string            `json:"commit_sha"`
	Lane       string            `json:"lane"`
	Services   []string          `json:"services"`             // 本次参与的服务
	Status     PipelineRunStatus `json:"status"`
	Message    string            `json:"message,omitempty"`
	Stages     []StageRun        `json:"stages,omitempty"`     // 嵌套返回（查详情时）
	CreatedAt  time.Time         `json:"created_at"`
	UpdatedAt  time.Time         `json:"updated_at"`
}

// StageRun 是 pipeline 中的一个阶段。
type StageRun struct {
	ID            string            `json:"id"`
	PipelineRunID string            `json:"pipeline_run_id"`
	Stage         StageType         `json:"stage"`
	Seq           int               `json:"seq"`
	Status        PipelineRunStatus `json:"status"`
	Message       string            `json:"message,omitempty"`
	Jobs          []JobRun          `json:"jobs,omitempty"` // 嵌套返回
	CreatedAt     time.Time         `json:"created_at"`
	UpdatedAt     time.Time         `json:"updated_at"`
}

// JobRun 是阶段内单个作业（某个服务的单测/构建/部署）。
type JobRun struct {
	ID         string            `json:"id"`
	StageRunID string            `json:"stage_run_id"`
	Name       string            `json:"name"`     // 服务名
	JobType    string            `json:"job_type"` // unit-test / build / deploy / e2e-http / e2e-lark
	RefID      string            `json:"ref_id,omitempty"`      // 关联 Build.ID 或 Release.ID
	K8sJobName string            `json:"k8s_job_name,omitempty"`
	Status     PipelineRunStatus `json:"status"`
	Log        string            `json:"log,omitempty"`
	CreatedAt  time.Time         `json:"created_at"`
	UpdatedAt  time.Time         `json:"updated_at"`
}
