package domain

import "time"

// App 代表一个应用定义，是 PaaS 引擎的核心管理单元。
// App 本身不映射到任何 K8s 资源，仅作为逻辑锚点。
type App struct {
	Name              string            `json:"name"`
	Description       string            `json:"description,omitempty"`
	ImageRepoName     string            `json:"image_repo"`       // 关联的 ImageRepo 名称
	Port              int               `json:"port"`             // 容器暴露端口，0 表示 Worker（不暴露端口、不创建 Service/VirtualService）
	ServiceAccount    string            `json:"service_account,omitempty"`
	Command           []string          `json:"command,omitempty"`
	EnvFromSecrets    []string          `json:"env_from_secrets,omitempty"`
	EnvFromConfigMaps []string          `json:"env_from_config_maps,omitempty"`
	Envs              map[string]string `json:"envs,omitempty"`
	CreatedAt         time.Time         `json:"created_at"`
	UpdatedAt         time.Time         `json:"updated_at"`
}
