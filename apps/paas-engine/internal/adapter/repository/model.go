package repository

import "time"

// AppModel 是 App 的数据库持久化模型。
type AppModel struct {
	Name              string `gorm:"primaryKey"`
	Description       string
	ImageRepoName     string
	Port              int
	ServiceAccount    string
	Command           string // JSON 序列化的 []string
	EnvFromSecrets    string // JSON 序列化的 []string
	EnvFromConfigMaps string // JSON 序列化的 []string
	Envs              string // JSON 序列化的 map[string]string
	ConfigBundles     string // JSON 序列化的 []string
	CreatedAt         time.Time
	UpdatedAt         time.Time
}

func (AppModel) TableName() string { return "apps" }

// ImageRepoModel 是 ImageRepo 的数据库持久化模型。
type ImageRepoModel struct {
	Name       string `gorm:"primaryKey"`
	Registry   string
	GitRepo    string
	ContextDir string
	Dockerfile string
	NoCache    bool
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

func (ImageRepoModel) TableName() string { return "image_repos" }

// BuildModel 是 Build 的数据库持久化模型。
type BuildModel struct {
	ID            string `gorm:"primaryKey"`
	ImageRepoName string `gorm:"index"`
	GitRef        string
	ImageTag      string
	Version       string
	Channel       string
	Status        string
	JobName       string
	Log           string `gorm:"type:text"`
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

func (BuildModel) TableName() string { return "builds" }

// ReleaseModel 是 Release 的数据库持久化模型。
type ReleaseModel struct {
	ID         string `gorm:"primaryKey"`
	AppName    string `gorm:"uniqueIndex:idx_app_lane"`
	Lane       string `gorm:"uniqueIndex:idx_app_lane"`
	Image      string
	Replicas   int32
	Envs       string // JSON 序列化
	Version    string // 自定义版本标识，用于环境变量注入
	Status     string
	Message    string `gorm:"type:text"`
	DeployName string
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

func (ReleaseModel) TableName() string { return "releases" }

// CIConfigModel 是 CIConfig 的数据库持久化模型。
type CIConfigModel struct {
	ID        string `gorm:"primaryKey"`
	Lane      string `gorm:"uniqueIndex"`
	Branch    string `gorm:"index"`
	Services  string // JSON 序列化的 []string
	Status    string
	CreatedAt time.Time
	UpdatedAt time.Time
}

func (CIConfigModel) TableName() string { return "ci_configs" }

// PipelineRunModel 是 PipelineRun 的数据库持久化模型。
type PipelineRunModel struct {
	ID         string `gorm:"primaryKey"`
	CIConfigID string `gorm:"index"`
	GitRef     string
	CommitSHA  string `gorm:"index"`
	Lane       string `gorm:"index"`
	Services   string // JSON 序列化的 []string
	Status     string
	Message    string `gorm:"type:text"`
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

func (PipelineRunModel) TableName() string { return "pipeline_runs" }

// StageRunModel 是 StageRun 的数据库持久化模型。
type StageRunModel struct {
	ID            string `gorm:"primaryKey"`
	PipelineRunID string `gorm:"index"`
	Stage         string
	Seq           int
	Status        string
	Message       string `gorm:"type:text"`
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

func (StageRunModel) TableName() string { return "stage_runs" }

// JobRunModel 是 JobRun 的数据库持久化模型。
type JobRunModel struct {
	ID         string `gorm:"primaryKey"`
	StageRunID string `gorm:"index"`
	Name       string
	JobType    string
	RefID      string
	K8sJobName string
	Status     string
	Log        string `gorm:"type:text"`
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

func (JobRunModel) TableName() string { return "job_runs" }

// ConfigBundleModel 是 ConfigBundle 的数据库持久化模型。
type ConfigBundleModel struct {
	Name          string `gorm:"primaryKey"`
	Description   string
	Keys          string // JSON serialized map[string]string
	LaneOverrides string // JSON serialized map[string]map[string]string
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

func (ConfigBundleModel) TableName() string { return "config_bundles" }

// DbMutationModel 记录一条待审批的 DDL/DML 申请。
type DbMutationModel struct {
	ID          uint       `gorm:"primaryKey;autoIncrement" json:"id"`
	DB          string     `gorm:"not null" json:"db"`
	SQL         string     `gorm:"not null;type:text" json:"sql"`
	Reason      string     `gorm:"type:text" json:"reason"`
	Status      string     `gorm:"not null;default:'pending'" json:"status"`
	SubmittedBy string     `gorm:"not null" json:"submitted_by"`
	ReviewedBy  string     `json:"reviewed_by"`
	ReviewNote  string     `gorm:"type:text" json:"review_note"`
	ExecutedAt  *time.Time `json:"executed_at"`
	Error       string     `gorm:"type:text" json:"error"`
	CreatedAt   time.Time  `json:"created_at"`
	UpdatedAt   time.Time  `json:"updated_at"`
}

func (DbMutationModel) TableName() string { return "db_mutations" }
