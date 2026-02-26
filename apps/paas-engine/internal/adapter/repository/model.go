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
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

func (ImageRepoModel) TableName() string { return "image_repos" }

// LaneModel 是 Lane 的数据库持久化模型。
type LaneModel struct {
	Name        string `gorm:"primaryKey"`
	Description string
	CreatedAt   time.Time
	UpdatedAt   time.Time
}

func (LaneModel) TableName() string { return "lanes" }

// BuildModel 是 Build 的数据库持久化模型。
type BuildModel struct {
	ID            string `gorm:"primaryKey"`
	ImageRepoName string `gorm:"index"`
	GitRef        string
	ImageTag      string
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
	Status     string
	DeployName string
	CreatedAt  time.Time
	UpdatedAt  time.Time
}

func (ReleaseModel) TableName() string { return "releases" }
