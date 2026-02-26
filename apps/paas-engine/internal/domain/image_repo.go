package domain

import (
	"fmt"
	"time"
)

// ImageRepo 代表一个镜像仓库的构建配置，与运行时 App 解耦。
// 多个 App 可以共享同一个 ImageRepo（如 worker 共享主服务镜像）。
type ImageRepo struct {
	Name       string    `json:"name"`
	Registry   string    `json:"registry"`    // 镜像仓库地址前缀，如 harbor.local/inner-bot/agent-service
	GitRepo    string    `json:"git_repo"`    // Git 仓库地址
	ContextDir string    `json:"context_dir"` // 构建上下文子目录
	CreatedAt  time.Time `json:"created_at"`
	UpdatedAt  time.Time `json:"updated_at"`
}

// FullImageRef 拼出完整镜像引用：registry:tag。
func (ir *ImageRepo) FullImageRef(tag string) string {
	return fmt.Sprintf("%s:%s", ir.Registry, tag)
}
