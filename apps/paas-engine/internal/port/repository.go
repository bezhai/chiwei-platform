package port

import (
	"context"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

type AppRepository interface {
	Save(ctx context.Context, app *domain.App) error
	FindByName(ctx context.Context, name string) (*domain.App, error)
	FindAll(ctx context.Context) ([]*domain.App, error)
	Update(ctx context.Context, app *domain.App) error
	Delete(ctx context.Context, name string) error
}

type BuildRepository interface {
	Save(ctx context.Context, build *domain.Build) error
	FindByID(ctx context.Context, id string) (*domain.Build, error)
	FindByImageRepo(ctx context.Context, imageRepoName string) ([]*domain.Build, error)
	FindLatestSuccessful(ctx context.Context, imageRepoName string) (*domain.Build, error)
	FindLatestVersioned(ctx context.Context, imageRepoName string) (*domain.Build, error)
	FindByImageTag(ctx context.Context, imageTag string) (*domain.Build, error)
	Update(ctx context.Context, build *domain.Build) error
}

type ImageRepoRepository interface {
	Save(ctx context.Context, repo *domain.ImageRepo) error
	FindByName(ctx context.Context, name string) (*domain.ImageRepo, error)
	FindAll(ctx context.Context) ([]*domain.ImageRepo, error)
	Update(ctx context.Context, repo *domain.ImageRepo) error
	Delete(ctx context.Context, name string) error
}

type ReleaseRepository interface {
	Save(ctx context.Context, release *domain.Release) error
	FindByID(ctx context.Context, id string) (*domain.Release, error)
	FindByAppAndLane(ctx context.Context, appName, lane string) (*domain.Release, error)
	FindAll(ctx context.Context, appName, lane string) ([]*domain.Release, error)
	Update(ctx context.Context, release *domain.Release) error
	Delete(ctx context.Context, id string) error
}

type ConfigBundleRepository interface {
	Save(ctx context.Context, bundle *domain.ConfigBundle) error
	FindByName(ctx context.Context, name string) (*domain.ConfigBundle, error)
	FindAll(ctx context.Context) ([]*domain.ConfigBundle, error)
	FindByNames(ctx context.Context, names []string) ([]*domain.ConfigBundle, error)
	Update(ctx context.Context, bundle *domain.ConfigBundle) error
	Delete(ctx context.Context, name string) error
}

type DynamicConfigRepository interface {
	Upsert(ctx context.Context, config *domain.DynamicConfig) error
	FindByKeyAndLane(ctx context.Context, key, lane string) (*domain.DynamicConfig, error)
	FindByLane(ctx context.Context, lane string) ([]*domain.DynamicConfig, error)
	FindAll(ctx context.Context) ([]*domain.DynamicConfig, error)
	DeleteByKeyAndLane(ctx context.Context, key, lane string) error
	DeleteByKey(ctx context.Context, key string) error
}

type GatewayRuleRepository interface {
	// Upsert 以 name 为冲突 key，存在则覆盖（管理 API PUT 用）。
	Upsert(ctx context.Context, rule *domain.GatewayRule) error
	// InsertIfAbsent 仅插入：name 已存在则 do-nothing、不覆盖（基线 ensure 用，
	// 关掉 Upsert 的 TOCTOU 覆盖窗口，绝不冲掉人工编辑/并发插入）。
	InsertIfAbsent(ctx context.Context, rule *domain.GatewayRule) error
	FindByName(ctx context.Context, name string) (*domain.GatewayRule, error)
	FindAll(ctx context.Context) ([]*domain.GatewayRule, error)
	Delete(ctx context.Context, name string) error

	// Tx 在一个 DB 事务内执行 fn，传入一个事务作用域的 repo。
	// 每次规则写操作（含 delete / rollback）都用它把"改规则 + 分配新
	// snapshot_version + 写快照"包成一个原子单元，保证 snapshot version 单调
	// 前进、历史与当前一致、并发写不串版。
	Tx(ctx context.Context, fn func(repo GatewayRuleRepository) error) error
	// SaveSnapshot 在当前（事务）作用域内分配下一个独立单调 snapshot_version，
	// 把传入的完整规则集连同 createdBy/reason 落一条历史，返回分配到的版本号。
	SaveSnapshot(ctx context.Context, rules []domain.GatewayRule, createdBy, reason string) (int64, error)
	// LatestSnapshotVersion 返回最新的 snapshot_version（无快照时返回 0）。
	// 它是 api-gateway 拉取快照时 version 的唯一来源。
	LatestSnapshotVersion(ctx context.Context) (int64, error)
	// ListSnapshots 按 snapshot_version 倒序返回最近 limit 条历史快照。
	ListSnapshots(ctx context.Context, limit int) ([]*domain.GatewayRuleSnapshot, error)
	// GetSnapshot 按版本号取一条历史快照，不存在返回 ErrGatewayRuleNotFound。
	GetSnapshot(ctx context.Context, version int64) (*domain.GatewayRuleSnapshot, error)
}
