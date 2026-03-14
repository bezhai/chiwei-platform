package domain

// PipelineConfig 对应 pipeline.yml 的解析结果。
type PipelineConfig struct {
	Services map[string]ServiceTestConfig `json:"services"`
	LarkFlow *LarkFlowConfig              `json:"lark_flow,omitempty"`
}

// ServiceTestConfig 描述单个服务的测试命令。
type ServiceTestConfig struct {
	Runtime  string            `json:"runtime"`
	UnitTest string            `json:"unit_test,omitempty"`
	E2ETest  string            `json:"e2e_test,omitempty"`
	E2EEnv   map[string]string `json:"e2e_env,omitempty"`
}

// LarkFlowConfig 描述飞书全链路 E2E 测试。
type LarkFlowConfig struct {
	Runtime string            `json:"runtime"`
	Cmd     string            `json:"cmd"`
	Timeout string            `json:"timeout,omitempty"`
	Env     map[string]string `json:"env,omitempty"`
}
