package domain

import "time"

// BuildStatus 是 Build 的状态机枚举。
// 状态流转：Pending → Running → (Succeeded | Failed | Cancelled)
type BuildStatus string

const (
	BuildStatusPending   BuildStatus = "pending"
	BuildStatusRunning   BuildStatus = "running"
	BuildStatusSucceeded BuildStatus = "succeeded"
	BuildStatusFailed    BuildStatus = "failed"
	BuildStatusCancelled BuildStatus = "cancelled"
)

func (s BuildStatus) IsTerminal() bool {
	return s == BuildStatusSucceeded || s == BuildStatusFailed || s == BuildStatusCancelled
}

// Build 代表一次镜像构建任务，对应 K8s 中的 Kaniko Job。
type Build struct {
	ID            string      `json:"id"`
	ImageRepoName string      `json:"image_repo_name"`
	GitRef        string      `json:"git_ref"` // branch / tag / commit
	ImageTag      string      `json:"image_tag"`
	Status        BuildStatus `json:"status"`
	JobName       string      `json:"job_name,omitempty"`
	Log           string      `json:"log,omitempty"`
	CreatedAt     time.Time   `json:"created_at"`
	UpdatedAt     time.Time   `json:"updated_at"`
}

// CanCancel 判断当前状态是否允许取消。
func (b *Build) CanCancel() bool {
	return b.Status == BuildStatusPending || b.Status == BuildStatusRunning
}
