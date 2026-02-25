package domain

import "time"

// App 代表一个应用定义，是 PaaS 引擎的核心管理单元。
// App 本身不映射到任何 K8s 资源，仅作为逻辑锚点。
type App struct {
	Name        string            `json:"name"`
	Description string            `json:"description,omitempty"`
	Image       string            `json:"image"` // 默认镜像仓库地址前缀
	Port           int               `json:"port"`                      // 容器暴露端口
	ServiceAccount string            `json:"service_account,omitempty"` // K8s ServiceAccount 名称
	EnvFromSecrets []string          `json:"env_from_secrets,omitempty"`
	Envs           map[string]string `json:"envs,omitempty"`
	CreatedAt   time.Time         `json:"created_at"`
	UpdatedAt   time.Time         `json:"updated_at"`
}
