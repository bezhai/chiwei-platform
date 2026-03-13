package domain

import "time"

const DefaultLane = "prod"

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
	Version    string            `json:"version,omitempty"` // 自定义版本标识，将注入为 VERSION 环境变量
	Status     ReleaseStatus     `json:"status"`
	Message    string            `json:"message,omitempty"`     // 部署失败原因
	DeployName string            `json:"deploy_name,omitempty"` // K8s Deployment 名称
	CreatedAt  time.Time         `json:"created_at"`
	UpdatedAt  time.Time         `json:"updated_at"`
}

// DeploymentStatus 表示 Deployment 的运行时状态。
type DeploymentStatus struct {
	DeployName string      `json:"deploy_name"`
	Desired    int32       `json:"desired"`
	Ready      int32       `json:"ready"`
	Available  int32       `json:"available"`
	Pods       []PodStatus `json:"pods"`
}

// PodStatus 表示单个 Pod 的运行时状态。
type PodStatus struct {
	Name     string `json:"name"`
	Status   string `json:"status"`
	Ready    bool   `json:"ready"`
	Restarts int32  `json:"restarts"`
	Reason   string `json:"reason,omitempty"`
}

// ResourceName 返回该 Release 对应的 K8s 资源名称（Deployment/Service 共用）。
func (r *Release) ResourceName() string {
	return r.AppName + "-" + r.Lane
}
