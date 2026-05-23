package domain

import "time"

// GatewayRule 表示一条 api-gateway 动态路由规则。
// Name 是业务主键 + 幂等 key；match / targets 在 DB 用 jsonb 列存。
type GatewayRule struct {
	Name        string          `json:"name"`
	Enabled     bool            `json:"enabled"`
	Priority    int             `json:"priority"`
	PathPrefix  string          `json:"path_prefix"`
	RequestLane string          `json:"request_lane,omitempty"`
	Match       GatewayMatch    `json:"match"`
	Targets     []GatewayTarget `json:"targets"`
	CreatedAt   time.Time       `json:"created_at"`
	UpdatedAt   time.Time       `json:"updated_at"`
	Version     int64           `json:"version"`
}

// GatewayMatch 是规则的匹配条件。
// PathPrefix / RequestLane 是 MVP 支持的匹配维度；
// Method/Headers/Query/Cookies 在 schema 中保留但校验器一律 reject（二期再开）。
type GatewayMatch struct {
	PathPrefix  string            `json:"path_prefix"`
	RequestLane string            `json:"request_lane,omitempty"`
	Method      string            `json:"method,omitempty"`
	Headers     map[string]string `json:"headers,omitempty"`
	Query       map[string]string `json:"query,omitempty"`
	Cookies     map[string]string `json:"cookies,omitempty"`
}

// GatewayTarget 是规则的转发目标。支持多 target 加权分流，weight 总和须为 100。
type GatewayTarget struct {
	Service       string `json:"service"`
	Lane          string `json:"lane"`
	Port          int    `json:"port"`
	Weight        int    `json:"weight"`
	StripPrefix   string `json:"strip_prefix,omitempty"`
	RewritePrefix string `json:"rewrite_prefix,omitempty"`
}

// GatewaySnapshot 是 /internal/gateway-rules 返回的完整快照。
// Version 是 snapshot 级单调 int（取 max(rule.Version)），api-gateway 仅用于日志 / metric label。
type GatewaySnapshot struct {
	Version   int64         `json:"version"`
	UpdatedAt time.Time     `json:"updated_at"`
	Rules     []GatewayRule `json:"rules"`
}
