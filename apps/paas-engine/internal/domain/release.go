package domain

import "time"

// ReleaseStatus 是 Release 的部署状态。
type ReleaseStatus string

const (
	ReleaseStatusPending  ReleaseStatus = "pending"
	ReleaseStatusDeployed ReleaseStatus = "deployed"
	ReleaseStatusFailed   ReleaseStatus = "failed"
)

// Release 代表一个 App 在某条 Lane 上的部署快照。
// 唯一约束：AppName + Lane 组合唯一，即一个 App 在一条 Lane 上只有一个活跃 Release。
// K8s 资源映射：Deployment + Service（名称格式 {app}-{lane}）
type Release struct {
	ID         string            `json:"id"`
	AppName    string            `json:"app_name"`
	Lane       string            `json:"lane"`
	Image      string            `json:"image"` // 完整镜像地址，含 tag
	Replicas   int32             `json:"replicas"`
	Envs       map[string]string `json:"envs,omitempty"`
	Status     ReleaseStatus     `json:"status"`
	DeployName string            `json:"deploy_name,omitempty"` // K8s Deployment 名称
	CreatedAt  time.Time         `json:"created_at"`
	UpdatedAt  time.Time         `json:"updated_at"`
}

// ResourceName 返回该 Release 对应的 K8s 资源名称（Deployment/Service 共用）。
func (r *Release) ResourceName() string {
	return r.AppName + "-" + r.Lane
}
