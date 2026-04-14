package domain

import "time"

// DynamicConfig 表示一条动态配置项。
// PK 为 (Key, Lane)，lane="prod" 是基线值，其他 lane 是覆盖。
type DynamicConfig struct {
	Key       string    `json:"key"`
	Lane      string    `json:"lane"`
	Value     string    `json:"value"`
	UpdatedAt time.Time `json:"updated_at"`
}
