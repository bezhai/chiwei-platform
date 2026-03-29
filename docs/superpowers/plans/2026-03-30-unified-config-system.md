# Unified Config System (ConfigBundle) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add ConfigBundle to PaaS Engine — a unified config management system that replaces fragmented env vars + K8s Secret/ConfigMap with grouped, lane-overridable, API-managed configuration bundles.

**Architecture:** New domain aggregate `ConfigBundle` with CRUD API, integrated into deploy flow via K8s Secret generation. App references bundles by name, deployer resolves bundle→lane override→app.Envs→release.Envs and writes a single auto-managed K8s Secret per app-lane. Existing env/secret fields preserved for backward compatibility during migration.

**Tech Stack:** Go, chi router, GORM/PostgreSQL, K8s client-go

---

## File Map

| Action | File | Responsibility |
|--------|------|---------------|
| Create | `internal/domain/config_bundle.go` | ConfigBundle + ConfigKey domain types |
| Modify | `internal/domain/errors.go` | Add ErrConfigBundleNotFound |
| Modify | `internal/port/repository.go` | Add ConfigBundleRepository interface |
| Create | `internal/adapter/repository/config_bundle_repo.go` | GORM implementation |
| Modify | `internal/adapter/repository/model.go` | Add ConfigBundleModel |
| Modify | `internal/adapter/repository/db.go` | AutoMigrate new model |
| Create | `internal/service/config_bundle_service.go` | CRUD, keys, lane overrides, generate, resolve |
| Create | `internal/service/config_bundle_service_test.go` | Tests with stub repos |
| Modify | `internal/domain/app.go` | Add ConfigBundles field |
| Modify | `internal/adapter/repository/app_repo.go` | Handle ConfigBundles serialization |
| Modify | `internal/service/app_service.go` | Conflict detection on bundle binding |
| Modify | `internal/service/app_service_test.go` | Tests for conflict detection |
| Create | `internal/adapter/http/config_bundle_handler.go` | HTTP handlers for ConfigBundle API |
| Modify | `internal/adapter/http/router.go` | Add /config-bundles routes + /resolved-config |
| Modify | `internal/port/kubernetes.go` | Add bundleEnvs param to Deployer.Deploy |
| Modify | `internal/adapter/kubernetes/deployer.go` | K8s Secret creation + envFrom injection |
| Modify | `internal/adapter/kubernetes/deployer_test.go` | Update tests |
| Modify | `internal/service/release_service.go` | Resolve bundle envs before deploy |
| Modify | `internal/service/release_service_test.go` | Update stub deployer + add tests |
| Modify | `internal/config/config.go` | (No change needed for v1) |
| Modify | `cmd/paas-engine/main.go` | Wire ConfigBundleRepo + Service + Handler |

---

### Task 1: Domain Model + Port Interface

**Files:**
- Create: `internal/domain/config_bundle.go`
- Modify: `internal/domain/errors.go`
- Modify: `internal/port/repository.go`

- [ ] **Step 1: Create ConfigBundle domain type**

```go
// internal/domain/config_bundle.go
package domain

import "time"

// ConfigBundle 表示一组按基础设施实例分组的配置项。
// 每个 key 是最终注入容器的环境变量名（如 PG_MAIN_HOST）。
type ConfigBundle struct {
	Name          string                       `json:"name"`
	Description   string                       `json:"description,omitempty"`
	Keys          map[string]string            `json:"keys,omitempty"`            // env var name → value
	LaneOverrides map[string]map[string]string `json:"lane_overrides,omitempty"`  // lane → {key: value}
	ReferencedBy  []string                     `json:"referenced_by,omitempty"`   // app names (populated on read)
	CreatedAt     time.Time                    `json:"created_at"`
	UpdatedAt     time.Time                    `json:"updated_at"`
}
```

- [ ] **Step 2: Add error sentinel**

In `internal/domain/errors.go`, add after `ErrImageRepoNotFound`:

```go
ErrConfigBundleNotFound = fmt.Errorf("config bundle %w", ErrNotFound)
```

- [ ] **Step 3: Add ConfigBundleRepository interface**

In `internal/port/repository.go`, add:

```go
type ConfigBundleRepository interface {
	Save(ctx context.Context, bundle *domain.ConfigBundle) error
	FindByName(ctx context.Context, name string) (*domain.ConfigBundle, error)
	FindAll(ctx context.Context) ([]*domain.ConfigBundle, error)
	FindByNames(ctx context.Context, names []string) ([]*domain.ConfigBundle, error)
	Update(ctx context.Context, bundle *domain.ConfigBundle) error
	Delete(ctx context.Context, name string) error
}
```

- [ ] **Step 4: Commit**

```bash
cd apps/paas-engine
git add internal/domain/config_bundle.go internal/domain/errors.go internal/port/repository.go
git commit -m "feat(config): add ConfigBundle domain model and repository port"
```

---

### Task 2: Repository Layer (DB Model + GORM Repo + Migration)

**Files:**
- Modify: `internal/adapter/repository/model.go`
- Create: `internal/adapter/repository/config_bundle_repo.go`
- Modify: `internal/adapter/repository/db.go`

- [ ] **Step 1: Add ConfigBundleModel to model.go**

In `internal/adapter/repository/model.go`, add after `JobRunModel`:

```go
// ConfigBundleModel 是 ConfigBundle 的数据库持久化模型。
type ConfigBundleModel struct {
	Name          string `gorm:"primaryKey"`
	Description   string
	Keys          string // JSON 序列化的 map[string]string
	LaneOverrides string // JSON 序列化的 map[string]map[string]string
	CreatedAt     time.Time
	UpdatedAt     time.Time
}

func (ConfigBundleModel) TableName() string { return "config_bundles" }
```

- [ ] **Step 2: Create config_bundle_repo.go**

```go
// internal/adapter/repository/config_bundle_repo.go
package repository

import (
	"context"
	"encoding/json"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
)

var _ port.ConfigBundleRepository = (*ConfigBundleRepo)(nil)

type ConfigBundleRepo struct {
	db *gorm.DB
}

func NewConfigBundleRepo(db *gorm.DB) *ConfigBundleRepo {
	return &ConfigBundleRepo{db: db}
}

func (r *ConfigBundleRepo) Save(ctx context.Context, bundle *domain.ConfigBundle) error {
	m, err := bundleToModel(bundle)
	if err != nil {
		return err
	}
	result := r.db.WithContext(ctx).Create(m)
	if result.Error != nil {
		if isUniqueConstraintError(result.Error) {
			return domain.ErrAlreadyExists
		}
		return result.Error
	}
	return nil
}

func (r *ConfigBundleRepo) FindByName(ctx context.Context, name string) (*domain.ConfigBundle, error) {
	var m ConfigBundleModel
	result := r.db.WithContext(ctx).First(&m, "name = ?", name)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrConfigBundleNotFound
		}
		return nil, result.Error
	}
	return modelToBundle(&m)
}

func (r *ConfigBundleRepo) FindAll(ctx context.Context) ([]*domain.ConfigBundle, error) {
	var models []ConfigBundleModel
	if err := r.db.WithContext(ctx).Find(&models).Error; err != nil {
		return nil, err
	}
	bundles := make([]*domain.ConfigBundle, 0, len(models))
	for i := range models {
		b, err := modelToBundle(&models[i])
		if err != nil {
			return nil, err
		}
		bundles = append(bundles, b)
	}
	return bundles, nil
}

func (r *ConfigBundleRepo) FindByNames(ctx context.Context, names []string) ([]*domain.ConfigBundle, error) {
	if len(names) == 0 {
		return nil, nil
	}
	var models []ConfigBundleModel
	if err := r.db.WithContext(ctx).Where("name IN ?", names).Find(&models).Error; err != nil {
		return nil, err
	}
	bundles := make([]*domain.ConfigBundle, 0, len(models))
	for i := range models {
		b, err := modelToBundle(&models[i])
		if err != nil {
			return nil, err
		}
		bundles = append(bundles, b)
	}
	return bundles, nil
}

func (r *ConfigBundleRepo) Update(ctx context.Context, bundle *domain.ConfigBundle) error {
	m, err := bundleToModel(bundle)
	if err != nil {
		return err
	}
	return r.db.WithContext(ctx).Save(m).Error
}

func (r *ConfigBundleRepo) Delete(ctx context.Context, name string) error {
	return r.db.WithContext(ctx).Delete(&ConfigBundleModel{}, "name = ?", name).Error
}

func bundleToModel(b *domain.ConfigBundle) (*ConfigBundleModel, error) {
	keysJSON, err := json.Marshal(b.Keys)
	if err != nil {
		return nil, err
	}
	laneOverridesJSON, err := json.Marshal(b.LaneOverrides)
	if err != nil {
		return nil, err
	}
	return &ConfigBundleModel{
		Name:          b.Name,
		Description:   b.Description,
		Keys:          string(keysJSON),
		LaneOverrides: string(laneOverridesJSON),
		CreatedAt:     b.CreatedAt,
		UpdatedAt:     b.UpdatedAt,
	}, nil
}

func modelToBundle(m *ConfigBundleModel) (*domain.ConfigBundle, error) {
	var keys map[string]string
	if m.Keys != "" {
		if err := json.Unmarshal([]byte(m.Keys), &keys); err != nil {
			return nil, err
		}
	}
	var laneOverrides map[string]map[string]string
	if m.LaneOverrides != "" {
		if err := json.Unmarshal([]byte(m.LaneOverrides), &laneOverrides); err != nil {
			return nil, err
		}
	}
	return &domain.ConfigBundle{
		Name:          m.Name,
		Description:   m.Description,
		Keys:          keys,
		LaneOverrides: laneOverrides,
		CreatedAt:     m.CreatedAt,
		UpdatedAt:     m.UpdatedAt,
	}, nil
}
```

- [ ] **Step 3: Add ConfigBundleModel to AutoMigrate in db.go**

In `internal/adapter/repository/db.go`, add `&ConfigBundleModel{}` to the AutoMigrate call:

```go
if err := db.AutoMigrate(
	&AppModel{},
	&ImageRepoModel{},
	&BuildModel{},
	&ReleaseModel{},
	&CIConfigModel{},
	&PipelineRunModel{},
	&StageRunModel{},
	&JobRunModel{},
	&ConfigBundleModel{},
); err != nil {
```

- [ ] **Step 4: Run build to verify compilation**

```bash
cd apps/paas-engine && go build ./...
```

Expected: BUILD SUCCESS

- [ ] **Step 5: Commit**

```bash
git add internal/adapter/repository/config_bundle_repo.go internal/adapter/repository/model.go internal/adapter/repository/db.go
git commit -m "feat(config): add ConfigBundle repository with GORM persistence"
```

---

### Task 3: ConfigBundle Service — CRUD (TDD)

**Files:**
- Create: `internal/service/config_bundle_service.go`
- Create: `internal/service/config_bundle_service_test.go`

- [ ] **Step 1: Write stub repo for tests**

Create `internal/service/config_bundle_service_test.go`:

```go
package service

import (
	"context"
	"testing"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stub ConfigBundle repo ---

type stubConfigBundleRepo struct {
	bundles map[string]*domain.ConfigBundle
}

func newStubConfigBundleRepo() *stubConfigBundleRepo {
	return &stubConfigBundleRepo{bundles: make(map[string]*domain.ConfigBundle)}
}

func (r *stubConfigBundleRepo) Save(_ context.Context, b *domain.ConfigBundle) error {
	if _, exists := r.bundles[b.Name]; exists {
		return domain.ErrAlreadyExists
	}
	r.bundles[b.Name] = b
	return nil
}

func (r *stubConfigBundleRepo) FindByName(_ context.Context, name string) (*domain.ConfigBundle, error) {
	if b, ok := r.bundles[name]; ok {
		return b, nil
	}
	return nil, domain.ErrConfigBundleNotFound
}

func (r *stubConfigBundleRepo) FindAll(_ context.Context) ([]*domain.ConfigBundle, error) {
	result := make([]*domain.ConfigBundle, 0, len(r.bundles))
	for _, b := range r.bundles {
		result = append(result, b)
	}
	return result, nil
}

func (r *stubConfigBundleRepo) FindByNames(_ context.Context, names []string) ([]*domain.ConfigBundle, error) {
	result := make([]*domain.ConfigBundle, 0)
	for _, name := range names {
		if b, ok := r.bundles[name]; ok {
			result = append(result, b)
		}
	}
	return result, nil
}

func (r *stubConfigBundleRepo) Update(_ context.Context, b *domain.ConfigBundle) error {
	r.bundles[b.Name] = b
	return nil
}

func (r *stubConfigBundleRepo) Delete(_ context.Context, name string) error {
	delete(r.bundles, name)
	return nil
}

// --- CRUD tests ---

func TestCreateConfigBundle_Success(t *testing.T) {
	repo := newStubConfigBundleRepo()
	appRepo := &stubAppRepo{}
	svc := NewConfigBundleService(repo, appRepo, &stubReleaseRepo{})

	bundle, err := svc.CreateBundle(context.Background(), CreateBundleRequest{
		Name:        "pg-main",
		Description: "主 PostgreSQL",
		Keys:        map[string]string{"PG_MAIN_HOST": "postgres", "PG_MAIN_PORT": "5432"},
	})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bundle.Name != "pg-main" {
		t.Errorf("Name = %q, want %q", bundle.Name, "pg-main")
	}
	if bundle.Keys["PG_MAIN_HOST"] != "postgres" {
		t.Errorf("Keys[PG_MAIN_HOST] = %q, want %q", bundle.Keys["PG_MAIN_HOST"], "postgres")
	}
}

func TestCreateConfigBundle_InvalidName(t *testing.T) {
	repo := newStubConfigBundleRepo()
	svc := NewConfigBundleService(repo, &stubAppRepo{}, &stubReleaseRepo{})

	_, err := svc.CreateBundle(context.Background(), CreateBundleRequest{
		Name: "INVALID_NAME",
	})
	if err == nil {
		t.Error("expected error for invalid name")
	}
}

func TestCreateConfigBundle_Duplicate(t *testing.T) {
	repo := newStubConfigBundleRepo()
	svc := NewConfigBundleService(repo, &stubAppRepo{}, &stubReleaseRepo{})

	_, _ = svc.CreateBundle(context.Background(), CreateBundleRequest{Name: "pg-main"})
	_, err := svc.CreateBundle(context.Background(), CreateBundleRequest{Name: "pg-main"})
	if err == nil {
		t.Error("expected error for duplicate")
	}
}

func TestDeleteConfigBundle_ReferencedByApp(t *testing.T) {
	repo := newStubConfigBundleRepo()
	repo.bundles["pg-main"] = &domain.ConfigBundle{Name: "pg-main"}
	appRepo := &stubAppRepo{apps: []*domain.App{
		{Name: "myapp", ConfigBundles: []string{"pg-main"}},
	}}
	svc := NewConfigBundleService(repo, appRepo, &stubReleaseRepo{})

	err := svc.DeleteBundle(context.Background(), "pg-main")
	if err == nil {
		t.Error("expected error when bundle is referenced by app")
	}
}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd apps/paas-engine && go test ./internal/service/ -run TestCreate.*ConfigBundle -v 2>&1 | head -20
```

Expected: FAIL (NewConfigBundleService not defined)

- [ ] **Step 3: Implement ConfigBundle service CRUD**

Create `internal/service/config_bundle_service.go`:

```go
package service

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type ConfigBundleService struct {
	bundleRepo  port.ConfigBundleRepository
	appRepo     port.AppRepository
	releaseRepo port.ReleaseRepository
}

func NewConfigBundleService(
	bundleRepo port.ConfigBundleRepository,
	appRepo port.AppRepository,
	releaseRepo port.ReleaseRepository,
) *ConfigBundleService {
	return &ConfigBundleService{
		bundleRepo:  bundleRepo,
		appRepo:     appRepo,
		releaseRepo: releaseRepo,
	}
}

type CreateBundleRequest struct {
	Name        string            `json:"name"`
	Description string            `json:"description"`
	Keys        map[string]string `json:"keys"`
}

func (s *ConfigBundleService) CreateBundle(ctx context.Context, req CreateBundleRequest) (*domain.ConfigBundle, error) {
	if err := domain.ValidateK8sName(req.Name); err != nil {
		return nil, err
	}
	now := time.Now()
	bundle := &domain.ConfigBundle{
		Name:        req.Name,
		Description: req.Description,
		Keys:        req.Keys,
		CreatedAt:   now,
		UpdatedAt:   now,
	}
	if err := s.bundleRepo.Save(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

func (s *ConfigBundleService) GetBundle(ctx context.Context, name string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}
	// Populate referenced_by
	apps, err := s.appRepo.FindAll(ctx)
	if err != nil {
		return nil, err
	}
	for _, app := range apps {
		for _, bn := range app.ConfigBundles {
			if bn == name {
				bundle.ReferencedBy = append(bundle.ReferencedBy, app.Name)
				break
			}
		}
	}
	return bundle, nil
}

func (s *ConfigBundleService) ListBundles(ctx context.Context) ([]*domain.ConfigBundle, error) {
	return s.bundleRepo.FindAll(ctx)
}

func (s *ConfigBundleService) UpdateBundle(ctx context.Context, name string, body []byte) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}

	fields, err := ParseFields(body)
	if err != nil {
		return nil, domain.ErrInvalidInput
	}

	if err := ApplyField(fields, "description", &bundle.Description); err != nil {
		return nil, domain.ErrInvalidInput
	}

	// Keys: merge semantics (same as envs)
	bundle.Keys, err = MergeEnvs(bundle.Keys, fields["keys"])
	if err != nil {
		return nil, domain.ErrInvalidInput
	}

	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

func (s *ConfigBundleService) DeleteBundle(ctx context.Context, name string) error {
	if _, err := s.bundleRepo.FindByName(ctx, name); err != nil {
		return err
	}
	// Check no app references this bundle
	apps, err := s.appRepo.FindAll(ctx)
	if err != nil {
		return err
	}
	for _, app := range apps {
		for _, bn := range app.ConfigBundles {
			if bn == name {
				return fmt.Errorf("%w: bundle %q is referenced by app %q", domain.ErrCannotDelete, name, app.Name)
			}
		}
	}
	return s.bundleRepo.Delete(ctx, name)
}

// --- Key management ---

func (s *ConfigBundleService) SetKeys(ctx context.Context, name string, body []byte) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, name)
	if err != nil {
		return nil, err
	}
	bundle.Keys, err = MergeEnvs(bundle.Keys, json.RawMessage(body))
	if err != nil {
		return nil, domain.ErrInvalidInput
	}
	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

func (s *ConfigBundleService) DeleteKey(ctx context.Context, bundleName, keyName string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}
	delete(bundle.Keys, keyName)
	// Also remove from lane overrides
	for lane := range bundle.LaneOverrides {
		delete(bundle.LaneOverrides[lane], keyName)
		if len(bundle.LaneOverrides[lane]) == 0 {
			delete(bundle.LaneOverrides, lane)
		}
	}
	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

func (s *ConfigBundleService) GenerateKey(ctx context.Context, bundleName, keyName string, length int) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}
	if length <= 0 {
		length = 32
	}
	b := make([]byte, length)
	if _, err := rand.Read(b); err != nil {
		return nil, fmt.Errorf("generate random: %w", err)
	}
	if bundle.Keys == nil {
		bundle.Keys = make(map[string]string)
	}
	bundle.Keys[keyName] = hex.EncodeToString(b)
	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// --- Lane overrides ---

func (s *ConfigBundleService) SetLaneOverrides(ctx context.Context, bundleName, lane string, body []byte) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}
	if bundle.LaneOverrides == nil {
		bundle.LaneOverrides = make(map[string]map[string]string)
	}
	existing := bundle.LaneOverrides[lane]
	merged, err := MergeEnvs(existing, json.RawMessage(body))
	if err != nil {
		return nil, domain.ErrInvalidInput
	}
	if merged == nil || len(merged) == 0 {
		delete(bundle.LaneOverrides, lane)
	} else {
		bundle.LaneOverrides[lane] = merged
	}
	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

func (s *ConfigBundleService) DeleteLaneOverrides(ctx context.Context, bundleName, lane string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}
	delete(bundle.LaneOverrides, lane)
	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

func (s *ConfigBundleService) DeleteLaneOverrideKey(ctx context.Context, bundleName, lane, keyName string) (*domain.ConfigBundle, error) {
	bundle, err := s.bundleRepo.FindByName(ctx, bundleName)
	if err != nil {
		return nil, err
	}
	if overrides, ok := bundle.LaneOverrides[lane]; ok {
		delete(overrides, keyName)
		if len(overrides) == 0 {
			delete(bundle.LaneOverrides, lane)
		}
	}
	bundle.UpdatedAt = time.Now()
	if err := s.bundleRepo.Update(ctx, bundle); err != nil {
		return nil, err
	}
	return bundle, nil
}

// --- Resolve config ---

// ResolvedConfigEntry 表示一个已解析的配置项，带来源标注。
type ResolvedConfigEntry struct {
	Value  string `json:"value"`
	Source string `json:"source"`
}

// ResolveConfig 合并 app 的所有配置来源，返回最终的环境变量 map。
// 优先级（低→高）：bundle baseline → bundle lane override → app.Envs → release.Envs → auto-injected
func (s *ConfigBundleService) ResolveConfig(ctx context.Context, appName, lane string) (map[string]ResolvedConfigEntry, error) {
	app, err := s.appRepo.FindByName(ctx, appName)
	if err != nil {
		return nil, err
	}
	if lane == "" {
		lane = domain.DefaultLane
	}

	resolved := make(map[string]ResolvedConfigEntry)

	// 1. Bundle baseline + lane overrides
	if len(app.ConfigBundles) > 0 {
		bundles, err := s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
		if err != nil {
			return nil, err
		}
		for _, bundle := range bundles {
			for k, v := range bundle.Keys {
				resolved[k] = ResolvedConfigEntry{Value: v, Source: bundle.Name}
			}
			if overrides, ok := bundle.LaneOverrides[lane]; ok {
				for k, v := range overrides {
					resolved[k] = ResolvedConfigEntry{Value: v, Source: fmt.Sprintf("%s[lane:%s]", bundle.Name, lane)}
				}
			}
		}
	}

	// 2. App.Envs
	for k, v := range app.Envs {
		resolved[k] = ResolvedConfigEntry{Value: v, Source: "app"}
	}

	// 3. Release.Envs
	release, err := s.releaseRepo.FindByAppAndLane(ctx, appName, lane)
	if err == nil && release != nil {
		for k, v := range release.Envs {
			resolved[k] = ResolvedConfigEntry{Value: v, Source: "release"}
		}
		if release.Version != "" {
			resolved["VERSION"] = ResolvedConfigEntry{Value: release.Version, Source: "auto"}
		}
	}

	// 4. Auto-injected
	resolved["LANE"] = ResolvedConfigEntry{Value: lane, Source: "auto"}

	return resolved, nil
}

// ResolveBundleEnvs 仅解析 bundle 层的配置（baseline + lane override），用于部署注入。
// 不包含 app.Envs 和 release.Envs（由 deployer 单独处理）。
func (s *ConfigBundleService) ResolveBundleEnvs(ctx context.Context, app *domain.App, lane string) (map[string]string, error) {
	if len(app.ConfigBundles) == 0 {
		return nil, nil
	}
	bundles, err := s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
	if err != nil {
		return nil, err
	}
	resolved := make(map[string]string)
	for _, bundle := range bundles {
		for k, v := range bundle.Keys {
			resolved[k] = v
		}
		if overrides, ok := bundle.LaneOverrides[lane]; ok {
			for k, v := range overrides {
				resolved[k] = v
			}
		}
	}
	return resolved, nil
}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd apps/paas-engine && go test ./internal/service/ -run TestCreate.*ConfigBundle -v
```

Expected: PASS

- [ ] **Step 5: Add tests for key management and lane overrides**

Append to `internal/service/config_bundle_service_test.go`:

```go
func TestSetKeys_MergesWithExisting(t *testing.T) {
	repo := newStubConfigBundleRepo()
	repo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_MAIN_HOST": "postgres", "PG_MAIN_PORT": "5432"},
	}
	svc := NewConfigBundleService(repo, &stubAppRepo{}, &stubReleaseRepo{})

	bundle, err := svc.SetKeys(context.Background(), "pg-main", []byte(`{"PG_MAIN_USER":"chiwei"}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bundle.Keys["PG_MAIN_HOST"] != "postgres" {
		t.Errorf("existing key should be preserved")
	}
	if bundle.Keys["PG_MAIN_USER"] != "chiwei" {
		t.Errorf("new key should be added")
	}
}

func TestSetKeys_DeleteKeyWithNull(t *testing.T) {
	repo := newStubConfigBundleRepo()
	repo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_MAIN_HOST": "postgres", "PG_MAIN_PORT": "5432"},
	}
	svc := NewConfigBundleService(repo, &stubAppRepo{}, &stubReleaseRepo{})

	bundle, err := svc.SetKeys(context.Background(), "pg-main", []byte(`{"PG_MAIN_PORT":null}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, ok := bundle.Keys["PG_MAIN_PORT"]; ok {
		t.Error("PG_MAIN_PORT should be deleted")
	}
	if bundle.Keys["PG_MAIN_HOST"] != "postgres" {
		t.Error("PG_MAIN_HOST should be preserved")
	}
}

func TestDeleteKey_AlsoRemovesFromLaneOverrides(t *testing.T) {
	repo := newStubConfigBundleRepo()
	repo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_MAIN_HOST": "postgres", "PG_MAIN_PORT": "5432"},
		LaneOverrides: map[string]map[string]string{
			"dev": {"PG_MAIN_HOST": "dev-postgres"},
		},
	}
	svc := NewConfigBundleService(repo, &stubAppRepo{}, &stubReleaseRepo{})

	bundle, err := svc.DeleteKey(context.Background(), "pg-main", "PG_MAIN_HOST")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if _, ok := bundle.Keys["PG_MAIN_HOST"]; ok {
		t.Error("key should be deleted")
	}
	if _, ok := bundle.LaneOverrides["dev"]; ok {
		t.Error("lane override should be cleaned up")
	}
}

func TestSetLaneOverrides_Success(t *testing.T) {
	repo := newStubConfigBundleRepo()
	repo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_MAIN_HOST": "postgres"},
	}
	svc := NewConfigBundleService(repo, &stubAppRepo{}, &stubReleaseRepo{})

	bundle, err := svc.SetLaneOverrides(context.Background(), "pg-main", "dev", []byte(`{"PG_MAIN_HOST":"dev-pg"}`))
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if bundle.LaneOverrides["dev"]["PG_MAIN_HOST"] != "dev-pg" {
		t.Errorf("lane override not set")
	}
}

func TestGenerateKey_CreatesRandomValue(t *testing.T) {
	repo := newStubConfigBundleRepo()
	repo.bundles["inter-service-auth"] = &domain.ConfigBundle{
		Name: "inter-service-auth",
		Keys: map[string]string{},
	}
	svc := NewConfigBundleService(repo, &stubAppRepo{}, &stubReleaseRepo{})

	bundle, err := svc.GenerateKey(context.Background(), "inter-service-auth", "AUTH_SECRET", 32)
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	val := bundle.Keys["AUTH_SECRET"]
	if len(val) != 64 { // 32 bytes = 64 hex chars
		t.Errorf("generated value length = %d, want 64", len(val))
	}
}

func TestResolveConfig_FullMerge(t *testing.T) {
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"PG_MAIN_HOST": "postgres", "PG_MAIN_PORT": "5432"},
		LaneOverrides: map[string]map[string]string{
			"dev": {"PG_MAIN_HOST": "dev-postgres"},
		},
	}
	bundleRepo.bundles["redis"] = &domain.ConfigBundle{
		Name: "redis",
		Keys: map[string]string{"REDIS_HOST": "redis"},
	}

	appRepo := &stubAppRepo{app: &domain.App{
		Name:          "myapp",
		ConfigBundles: []string{"pg-main", "redis"},
		Envs:          map[string]string{"APP_NAME": "myapp"},
	}}

	releaseRepo := newReleaseTestReleaseRepo()
	_ = releaseRepo.Save(context.Background(), &domain.Release{
		ID:      "r1",
		AppName: "myapp",
		Lane:    "dev",
		Envs:    map[string]string{"DEBUG": "true"},
		Version: "1.0.0",
	})

	svc := NewConfigBundleService(bundleRepo, appRepo, releaseRepo)

	resolved, err := svc.ResolveConfig(context.Background(), "myapp", "dev")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}

	// Bundle baseline
	if resolved["PG_MAIN_PORT"].Value != "5432" || resolved["PG_MAIN_PORT"].Source != "pg-main" {
		t.Errorf("PG_MAIN_PORT: got %+v", resolved["PG_MAIN_PORT"])
	}
	// Lane override
	if resolved["PG_MAIN_HOST"].Value != "dev-postgres" || resolved["PG_MAIN_HOST"].Source != "pg-main[lane:dev]" {
		t.Errorf("PG_MAIN_HOST: got %+v", resolved["PG_MAIN_HOST"])
	}
	// Second bundle
	if resolved["REDIS_HOST"].Value != "redis" || resolved["REDIS_HOST"].Source != "redis" {
		t.Errorf("REDIS_HOST: got %+v", resolved["REDIS_HOST"])
	}
	// App envs
	if resolved["APP_NAME"].Value != "myapp" || resolved["APP_NAME"].Source != "app" {
		t.Errorf("APP_NAME: got %+v", resolved["APP_NAME"])
	}
	// Release envs
	if resolved["DEBUG"].Value != "true" || resolved["DEBUG"].Source != "release" {
		t.Errorf("DEBUG: got %+v", resolved["DEBUG"])
	}
	// Auto-injected
	if resolved["VERSION"].Value != "1.0.0" {
		t.Errorf("VERSION: got %+v", resolved["VERSION"])
	}
	if resolved["LANE"].Value != "dev" {
		t.Errorf("LANE: got %+v", resolved["LANE"])
	}
}
```

- [ ] **Step 6: Run all ConfigBundle tests**

```bash
cd apps/paas-engine && go test ./internal/service/ -run TestCreate.*ConfigBundle\|TestSet.*\|TestDelete.*Key\|TestGenerate.*\|TestResolve.* -v
```

Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add internal/service/config_bundle_service.go internal/service/config_bundle_service_test.go
git commit -m "feat(config): add ConfigBundle service with CRUD, keys, lane overrides, resolve"
```

---

### Task 4: App Model — Add ConfigBundles Field

**Files:**
- Modify: `internal/domain/app.go`
- Modify: `internal/adapter/repository/model.go`
- Modify: `internal/adapter/repository/app_repo.go`
- Modify: `internal/service/app_service.go`
- Modify: `internal/service/app_service_test.go`

- [ ] **Step 1: Add ConfigBundles to domain App**

In `internal/domain/app.go`, add after the `Envs` field:

```go
ConfigBundles []string `json:"config_bundles,omitempty"`
```

- [ ] **Step 2: Add ConfigBundles to AppModel**

In `internal/adapter/repository/model.go`, add to `AppModel` after `Envs`:

```go
ConfigBundles string // JSON 序列化的 []string
```

- [ ] **Step 3: Update appToModel/modelToApp in app_repo.go**

In `appToModel`, add after `envFromConfigMapsJSON`:

```go
configBundlesJSON, err := json.Marshal(a.ConfigBundles)
if err != nil {
	return nil, err
}
```

And add the field to the returned model:

```go
ConfigBundles: string(configBundlesJSON),
```

In `modelToApp`, add after the `envFromConfigMaps` unmarshaling:

```go
var configBundles []string
if m.ConfigBundles != "" {
	if err := json.Unmarshal([]byte(m.ConfigBundles), &configBundles); err != nil {
		return nil, err
	}
}
```

And add to the returned App:

```go
ConfigBundles: configBundles,
```

- [ ] **Step 4: Update AppService to handle config_bundles + conflict detection**

In `internal/service/app_service.go`:

Add `configBundleRepo` to the struct and constructor:

```go
type AppService struct {
	appRepo          port.AppRepository
	imageRepoRepo    port.ImageRepoRepository
	releaseRepo      port.ReleaseRepository
	configBundleRepo port.ConfigBundleRepository
}

func NewAppService(appRepo port.AppRepository, imageRepoRepo port.ImageRepoRepository, releaseRepo port.ReleaseRepository, configBundleRepo port.ConfigBundleRepository) *AppService {
	return &AppService{appRepo: appRepo, imageRepoRepo: imageRepoRepo, releaseRepo: releaseRepo, configBundleRepo: configBundleRepo}
}
```

Add `ConfigBundles` to `CreateAppRequest`:

```go
ConfigBundles []string `json:"config_bundles"`
```

In `CreateApp`, add before saving:

```go
if len(req.ConfigBundles) > 0 {
	if err := s.validateConfigBundles(ctx, req.ConfigBundles); err != nil {
		return nil, err
	}
}
app.ConfigBundles = req.ConfigBundles
```

In `UpdateApp`, add after the existing ApplyField calls:

```go
if err := ApplyField(fields, "config_bundles", &app.ConfigBundles); err != nil {
	return nil, domain.ErrInvalidInput
}
if _, ok := fields["config_bundles"]; ok && len(app.ConfigBundles) > 0 {
	if err := s.validateConfigBundles(ctx, app.ConfigBundles); err != nil {
		return nil, err
	}
}
```

Add the validation method:

```go
// validateConfigBundles checks that all referenced bundles exist and have no key conflicts.
func (s *AppService) validateConfigBundles(ctx context.Context, bundleNames []string) error {
	if s.configBundleRepo == nil {
		return nil
	}
	bundles, err := s.configBundleRepo.FindByNames(ctx, bundleNames)
	if err != nil {
		return err
	}
	if len(bundles) != len(bundleNames) {
		// Find missing bundle
		found := make(map[string]bool)
		for _, b := range bundles {
			found[b.Name] = true
		}
		for _, name := range bundleNames {
			if !found[name] {
				return fmt.Errorf("%w: config bundle %q not found", domain.ErrInvalidInput, name)
			}
		}
	}
	// Check key conflicts across bundles
	seen := make(map[string]string) // key name → bundle name
	for _, bundle := range bundles {
		for key := range bundle.Keys {
			if other, ok := seen[key]; ok {
				return fmt.Errorf("%w: key %q defined in both %q and %q", domain.ErrInvalidInput, key, other, bundle.Name)
			}
			seen[key] = bundle.Name
		}
	}
	return nil
}
```

Add `"fmt"` to imports if not already present.

- [ ] **Step 5: Update existing AppService tests to pass new parameter**

In `internal/service/app_service_test.go`, update all `NewAppService` calls to add a 4th parameter. Add a `stubConfigBundleRepo` field to tests that need it, or pass `nil`:

```go
// Update all existing calls from:
svc := NewAppService(appRepo, imageRepoRepo, &stubReleaseRepo{})
// To:
svc := NewAppService(appRepo, imageRepoRepo, &stubReleaseRepo{}, nil)
```

Also update `stubAppRepo` in `internal/service/log_service_test.go` (the shared stub) to support an `apps` field:

```go
type stubAppRepo struct {
	app  *domain.App
	apps []*domain.App // for FindAll with multiple apps
	err  error
}
```

And update its `FindAll` method:

```go
func (s *stubAppRepo) FindAll(_ context.Context) ([]*domain.App, error) {
	if s.apps != nil {
		return s.apps, nil
	}
	if s.app != nil {
		return []*domain.App{s.app}, nil
	}
	return nil, nil
}
```

Note: this stub is defined in `log_service_test.go` and shared across all service test files.

- [ ] **Step 6: Add conflict detection tests**

Append to `internal/service/app_service_test.go`:

```go
func TestUpdateApp_ConfigBundleConflict(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	bundleRepo := newStubConfigBundleRepo()
	bundleRepo.bundles["pg-main"] = &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"DB_HOST": "postgres"},
	}
	bundleRepo.bundles["pg-external"] = &domain.ConfigBundle{
		Name: "pg-external",
		Keys: map[string]string{"DB_HOST": "external-pg"}, // conflict!
	}
	svc := NewAppService(appRepo, &stubImageRepoRepo{}, &stubReleaseRepo{}, bundleRepo)

	_, err := svc.UpdateApp(context.Background(), "myapp",
		[]byte(`{"config_bundles":["pg-main","pg-external"]}`))
	if err == nil {
		t.Error("expected error for key conflict")
	}
}

func TestUpdateApp_ConfigBundleNotFound(t *testing.T) {
	appRepo := &stubAppRepo{app: &domain.App{Name: "myapp"}}
	bundleRepo := newStubConfigBundleRepo()
	svc := NewAppService(appRepo, &stubImageRepoRepo{}, &stubReleaseRepo{}, bundleRepo)

	_, err := svc.UpdateApp(context.Background(), "myapp",
		[]byte(`{"config_bundles":["nonexistent"]}`))
	if err == nil {
		t.Error("expected error for missing bundle")
	}
}
```

- [ ] **Step 7: Run all app and config bundle tests**

```bash
cd apps/paas-engine && go test ./internal/service/ -v
```

Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add internal/domain/app.go internal/adapter/repository/model.go internal/adapter/repository/app_repo.go internal/service/app_service.go internal/service/app_service_test.go internal/service/config_bundle_service_test.go internal/service/log_service_test.go
git commit -m "feat(config): add ConfigBundles field to App with conflict detection"
```

---

### Task 5: HTTP Handlers + Routes

**Files:**
- Create: `internal/adapter/http/config_bundle_handler.go`
- Modify: `internal/adapter/http/router.go`

- [ ] **Step 1: Create config_bundle_handler.go**

```go
// internal/adapter/http/config_bundle_handler.go
package http

import (
	"encoding/json"
	"io"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type ConfigBundleHandler struct {
	svc *service.ConfigBundleService
}

func NewConfigBundleHandler(svc *service.ConfigBundleService) *ConfigBundleHandler {
	return &ConfigBundleHandler{svc: svc}
}

func (h *ConfigBundleHandler) Create(w http.ResponseWriter, r *http.Request) {
	var req service.CreateBundleRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.CreateBundle(r.Context(), req)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusCreated, bundle)
}

func (h *ConfigBundleHandler) List(w http.ResponseWriter, r *http.Request) {
	bundles, err := h.svc.ListBundles(r.Context())
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundles)
}

func (h *ConfigBundleHandler) Get(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	bundle, err := h.svc.GetBundle(r.Context(), name)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) Update(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.UpdateBundle(r.Context(), name, body)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) Delete(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	if err := h.svc.DeleteBundle(r.Context(), name); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": name})
}

func (h *ConfigBundleHandler) SetKeys(w http.ResponseWriter, r *http.Request) {
	name := chi.URLParam(r, "bundle")
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.SetKeys(r.Context(), name, body)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) DeleteKey(w http.ResponseWriter, r *http.Request) {
	bundleName := chi.URLParam(r, "bundle")
	keyName := chi.URLParam(r, "key")
	bundle, err := h.svc.DeleteKey(r.Context(), bundleName, keyName)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) GenerateKey(w http.ResponseWriter, r *http.Request) {
	bundleName := chi.URLParam(r, "bundle")
	keyName := chi.URLParam(r, "key")

	length := 32
	if r.ContentLength > 0 {
		var req struct {
			Length int `json:"length"`
		}
		if err := json.NewDecoder(r.Body).Decode(&req); err == nil && req.Length > 0 {
			length = req.Length
		}
	}

	bundle, err := h.svc.GenerateKey(r.Context(), bundleName, keyName, length)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) SetLaneOverrides(w http.ResponseWriter, r *http.Request) {
	bundleName := chi.URLParam(r, "bundle")
	lane := chi.URLParam(r, "lane")
	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, err)
		return
	}
	bundle, err := h.svc.SetLaneOverrides(r.Context(), bundleName, lane, body)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) DeleteLaneOverrides(w http.ResponseWriter, r *http.Request) {
	bundleName := chi.URLParam(r, "bundle")
	lane := chi.URLParam(r, "lane")
	bundle, err := h.svc.DeleteLaneOverrides(r.Context(), bundleName, lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

func (h *ConfigBundleHandler) DeleteLaneOverrideKey(w http.ResponseWriter, r *http.Request) {
	bundleName := chi.URLParam(r, "bundle")
	lane := chi.URLParam(r, "lane")
	keyName := chi.URLParam(r, "key")
	bundle, err := h.svc.DeleteLaneOverrideKey(r.Context(), bundleName, lane, keyName)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, bundle)
}

// ResolveConfig is mounted on the App handler, but calls ConfigBundleService.
func (h *ConfigBundleHandler) ResolveConfig(w http.ResponseWriter, r *http.Request) {
	appName := chi.URLParam(r, "app")
	lane := r.URL.Query().Get("lane")
	resolved, err := h.svc.ResolveConfig(r.Context(), appName, lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, resolved)
}

}
```

Note: the `strconv` import is not needed in this file — do not include it.

- [ ] **Step 2: Add routes to router.go**

In `internal/adapter/http/router.go`, update the `NewRouter` function signature to accept the new handler:

```go
func NewRouter(
	appH *AppHandler,
	releaseH *ReleaseHandler,
	logH *LogHandler,
	imageRepoH *ImageRepoHandler,
	opsH *OpsHandler,
	pipelineH *PipelineHandler,
	configBundleH *ConfigBundleHandler,
	apiToken string,
) http.Handler {
```

Add routes inside the `/api/paas` route group, after the existing CI Pipeline block:

```go
// Config Bundles
r.Route("/config-bundles", func(r chi.Router) {
	r.Post("/", configBundleH.Create)
	r.Get("/", configBundleH.List)
	r.Route("/{bundle}", func(r chi.Router) {
		r.Get("/", configBundleH.Get)
		r.Put("/", configBundleH.Update)
		r.Delete("/", configBundleH.Delete)
		r.Put("/keys", configBundleH.SetKeys)
		r.Delete("/keys/{key}", configBundleH.DeleteKey)
		r.Post("/keys/{key}/generate", configBundleH.GenerateKey)
		r.Route("/lanes/{lane}", func(r chi.Router) {
			r.Put("/", configBundleH.SetLaneOverrides)
			r.Delete("/", configBundleH.DeleteLaneOverrides)
			r.Delete("/{key}", configBundleH.DeleteLaneOverrideKey)
		})
	})
})
```

Also add the resolved-config endpoint inside the existing `/{app}` route group (after `r.Get("/logs", logH.GetLogs)`):

```go
r.Get("/resolved-config", configBundleH.ResolveConfig)
```

- [ ] **Step 3: Build to verify compilation**

```bash
cd apps/paas-engine && go build ./...
```

Expected: FAIL — main.go needs to be updated to pass configBundleH (done in Task 7).

This is expected. We'll fix it in Task 7 (wiring). For now, just verify the handler + router code compiles in isolation:

```bash
cd apps/paas-engine && go vet ./internal/adapter/http/...
```

- [ ] **Step 4: Commit**

```bash
git add internal/adapter/http/config_bundle_handler.go internal/adapter/http/router.go
git commit -m "feat(config): add ConfigBundle HTTP handlers and routes"
```

---

### Task 6: Deployer — K8s Secret Management

**Files:**
- Modify: `internal/port/kubernetes.go`
- Modify: `internal/adapter/kubernetes/deployer.go`
- Modify: `internal/adapter/kubernetes/deployer_test.go`
- Modify: `internal/service/release_service.go`
- Modify: `internal/service/release_service_test.go`

- [ ] **Step 1: Update Deployer interface**

In `internal/port/kubernetes.go`, change the `Deploy` signature:

```go
Deploy(ctx context.Context, release *domain.Release, app *domain.App, bundleEnvs map[string]string) error
```

- [ ] **Step 2: Update K8sDeployer.Deploy to create K8s Secret + envFrom**

In `internal/adapter/kubernetes/deployer.go`:

Update `Deploy` method signature:

```go
func (d *K8sDeployer) Deploy(ctx context.Context, release *domain.Release, app *domain.App, bundleEnvs map[string]string) error {
```

Update `applyDeployment` signature and call:

```go
func (d *K8sDeployer) applyDeployment(ctx context.Context, release *domain.Release, app *domain.App, bundleEnvs map[string]string) error {
```

In `Deploy`, update the call:

```go
if err := d.applyDeployment(ctx, release, app, bundleEnvs); err != nil {
```

In `applyDeployment`, add K8s Secret creation before building the container. Replace the envFrom and env construction:

```go
// Bundle envs → auto-managed K8s Secret
var bundleSecretName string
if len(bundleEnvs) > 0 {
	bundleSecretName = name + "-config"
	if err := d.applySecret(ctx, bundleSecretName, bundleEnvs); err != nil {
		return fmt.Errorf("apply config secret: %w", err)
	}
}

mergedEnvs := mergeEnvs(app.Envs, release.Envs)
if release.Version != "" {
	mergedEnvs["VERSION"] = release.Version
}
mergedEnvs["LANE"] = release.Lane
envVars := envsToK8s(mergedEnvs)

// EnvFrom: legacy sources + bundle secret
envFrom := buildEnvFrom(app.EnvFromSecrets, app.EnvFromConfigMaps)
if bundleSecretName != "" {
	envFrom = append(envFrom, corev1.EnvFromSource{
		SecretRef: &corev1.SecretEnvSource{
			LocalObjectReference: corev1.LocalObjectReference{Name: bundleSecretName},
		},
	})
}

container := corev1.Container{
	Name:    app.Name,
	Image:   release.Image,
	EnvFrom: envFrom,
	Env:     envVars,
}
```

Add the `applySecret` method:

```go
func (d *K8sDeployer) applySecret(ctx context.Context, name string, data map[string]string) error {
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: d.namespace,
			Labels:    map[string]string{"managed-by": "paas-engine"},
		},
		StringData: data,
	}

	existing, err := d.client.CoreV1().Secrets(d.namespace).Get(ctx, name, metav1.GetOptions{})
	if errors.IsNotFound(err) {
		_, err = d.client.CoreV1().Secrets(d.namespace).Create(ctx, secret, metav1.CreateOptions{})
		return err
	}
	if err != nil {
		return err
	}
	existing.StringData = data
	_, err = d.client.CoreV1().Secrets(d.namespace).Update(ctx, existing, metav1.UpdateOptions{})
	return err
}
```

- [ ] **Step 3: Update stub deployer in release_service_test.go**

In `internal/service/release_service_test.go`, update `stubDeployer.Deploy`:

```go
func (s *stubDeployer) Deploy(_ context.Context, _ *domain.Release, _ *domain.App, _ map[string]string) error {
	return s.deployErr
}
```

- [ ] **Step 4: Update deployer_test.go if applicable**

In `internal/adapter/kubernetes/deployer_test.go`, update any calls to `Deploy` to pass the new `bundleEnvs` parameter (likely `nil` for existing tests):

Search for `.Deploy(` calls and add `nil` as the 4th argument.

- [ ] **Step 5: Update ReleaseService to resolve bundle envs before deploying**

In `internal/service/release_service.go`:

Add `configBundleSvc` to the struct and constructor:

```go
type ReleaseService struct {
	appRepo         port.AppRepository
	imageRepoRepo   port.ImageRepoRepository
	buildRepo       port.BuildRepository
	releaseRepo     port.ReleaseRepository
	deployer        port.Deployer
	configBundleSvc *ConfigBundleService
}

func NewReleaseService(
	appRepo port.AppRepository,
	imageRepoRepo port.ImageRepoRepository,
	buildRepo port.BuildRepository,
	releaseRepo port.ReleaseRepository,
	deployer port.Deployer,
	configBundleSvc *ConfigBundleService,
) *ReleaseService {
	return &ReleaseService{
		appRepo:         appRepo,
		imageRepoRepo:   imageRepoRepo,
		buildRepo:       buildRepo,
		releaseRepo:     releaseRepo,
		deployer:        deployer,
		configBundleSvc: configBundleSvc,
	}
}
```

In `CreateOrUpdateRelease`, before the deployer.Deploy call, resolve bundle envs:

```go
// Resolve bundle envs
var bundleEnvs map[string]string
if s.configBundleSvc != nil && len(app.ConfigBundles) > 0 {
	bundleEnvs, err = s.configBundleSvc.ResolveBundleEnvs(ctx, app, lane)
	if err != nil {
		return nil, fmt.Errorf("resolve config bundles: %w", err)
	}
}
```

Update the deployer.Deploy call:

```go
if err := s.deployer.Deploy(ctx, release, app, bundleEnvs); err != nil {
```

Do the same in `UpdateRelease` — add bundle env resolution before the deployer.Deploy call:

```go
var bundleEnvs map[string]string
if s.configBundleSvc != nil && len(app.ConfigBundles) > 0 {
	bundleEnvs, err = s.configBundleSvc.ResolveBundleEnvs(ctx, app, release.Lane)
	if err != nil {
		return nil, fmt.Errorf("resolve config bundles: %w", err)
	}
}

if s.deployer != nil {
	if err := s.deployer.Deploy(ctx, release, app, bundleEnvs); err != nil {
```

- [ ] **Step 6: Update NewReleaseService calls in tests**

In `internal/service/release_service_test.go`, update all `NewReleaseService` calls to add `nil` as the 6th parameter:

```go
// From:
svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer)
// To:
svc := NewReleaseService(appRepo, imageRepoRepo, &stubBuildRepo{}, releaseRepo, deployer, nil)
```

- [ ] **Step 7: Run all tests**

```bash
cd apps/paas-engine && go test ./internal/... -v
```

Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add internal/port/kubernetes.go internal/adapter/kubernetes/deployer.go internal/adapter/kubernetes/deployer_test.go internal/service/release_service.go internal/service/release_service_test.go
git commit -m "feat(config): integrate ConfigBundle into deploy flow with K8s Secret injection"
```

---

### Task 7: Wiring — main.go

**Files:**
- Modify: `cmd/paas-engine/main.go`

- [ ] **Step 1: Add ConfigBundleRepo, Service, and Handler**

In `cmd/paas-engine/main.go`:

After the existing repo declarations, add:

```go
configBundleRepo := repository.NewConfigBundleRepo(db)
```

After the existing service declarations, add:

```go
configBundleSvc := service.NewConfigBundleService(configBundleRepo, appRepo, releaseRepo)
```

Update `NewAppService` to pass `configBundleRepo`:

```go
appSvc := service.NewAppService(appRepo, imageRepoRepo, releaseRepo, configBundleRepo)
```

Update `NewReleaseService` to pass `configBundleSvc`:

```go
releaseSvc := service.NewReleaseService(appRepo, imageRepoRepo, buildRepo, releaseRepo, deployer, configBundleSvc)
```

Update `NewRouter` to pass the new handler:

```go
handler := httpadapter.NewRouter(
	httpadapter.NewAppHandler(appSvc, buildSvc),
	httpadapter.NewReleaseHandler(releaseSvc),
	httpadapter.NewLogHandler(logSvc),
	httpadapter.NewImageRepoHandler(imageRepoSvc),
	httpadapter.NewOpsHandler(opsDbs),
	httpadapter.NewPipelineHandler(pipelineSvc),
	httpadapter.NewConfigBundleHandler(configBundleSvc),
	cfg.APIToken,
)
```

- [ ] **Step 2: Build the entire project**

```bash
cd apps/paas-engine && go build ./...
```

Expected: BUILD SUCCESS

- [ ] **Step 3: Run all tests**

```bash
cd apps/paas-engine && go test ./... -v
```

Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add cmd/paas-engine/main.go
git commit -m "feat(config): wire ConfigBundle into application startup"
```

---

### Task 8: Update Pipeline Service Constructor

**Files:**
- Modify: `internal/service/pipeline_service.go` (or wherever NewPipelineService is called)

The `NewReleaseService` signature changed (added `configBundleSvc` param). If `NewPipelineService` constructs a `ReleaseService` internally or passes one around, it needs updating.

- [ ] **Step 1: Check if pipeline_service.go needs updates**

Check if `NewReleaseService` is called anywhere besides `main.go`:

```bash
cd apps/paas-engine && grep -r "NewReleaseService" --include="*.go" .
```

If only `main.go` calls it, this task is a no-op. If other files call it, update them to pass `nil` for `configBundleSvc`.

- [ ] **Step 2: Run full test suite**

```bash
cd apps/paas-engine && go test ./... -count=1
```

Expected: ALL PASS

- [ ] **Step 3: Commit (if changes needed)**

```bash
git add -A && git commit -m "fix: update NewReleaseService callers for new configBundleSvc param"
```

---

### Task 9: Final Verification

- [ ] **Step 1: Run go vet**

```bash
cd apps/paas-engine && go vet ./...
```

Expected: No issues

- [ ] **Step 2: Run full test suite with race detector**

```bash
cd apps/paas-engine && go test -race ./...
```

Expected: ALL PASS, no data races

- [ ] **Step 3: Verify build compiles cleanly**

```bash
cd apps/paas-engine && go build -o /dev/null ./cmd/paas-engine/
```

Expected: BUILD SUCCESS

- [ ] **Step 4: Commit any remaining fixes**

If any fixes were needed during verification, commit them.

- [ ] **Step 5: Review all changes**

```bash
git log --oneline main..HEAD
git diff --stat main..HEAD
```

Verify the commit history makes sense and all files are included.
