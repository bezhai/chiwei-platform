package domain

import "time"

// ConfigBundle 表示一组按基础设施实例分组的配置项。
// 每个 key 是最终注入容器的环境变量名（如 PG_MAIN_HOST）。
type ConfigBundle struct {
	Name          string                       `json:"name"`
	Description   string                       `json:"description,omitempty"`
	Keys          map[string]string            `json:"keys,omitempty"`
	LaneOverrides map[string]map[string]string `json:"lane_overrides,omitempty"`
	ReferencedBy  []string                     `json:"referenced_by,omitempty"`
	CreatedAt     time.Time                    `json:"created_at"`
	UpdatedAt     time.Time                    `json:"updated_at"`
}
