# Dynamic Config by Lane — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现按泳道隔离的运行时动态配置系统，支持 SDK 透明读取（10s 缓存）和 Dashboard 管理。

**Architecture:** 扩展 paas-engine 新增一张 `dynamic_configs` 表（PK: key+lane），提供管理 API（auth 保护）和内部读取端点（无 auth，供集群内 SDK 使用）。Python/TS SDK 各实现一个 `dynamic_config` 模块，通过 lane_provider 自动获取当前泳道，lazy 拉取全量快照并缓存 10s。Dashboard 通过 monitor-dashboard 后端代理调用管理 API。

**Tech Stack:** Go (paas-engine, GORM, chi), Python (httpx), TypeScript (Bun/Hono), React + Ant Design (dashboard)

**Spec:** `docs/superpowers/specs/2026-04-14-dynamic-config-by-lane-design.md`

---

### Task 1: paas-engine 数据层 — Domain + Port + Model + Repo

**Files:**
- Create: `apps/paas-engine/internal/domain/dynamic_config.go`
- Modify: `apps/paas-engine/internal/domain/errors.go` (追加 ErrDynamicConfigNotFound)
- Modify: `apps/paas-engine/internal/port/repository.go` (追加 DynamicConfigRepository 接口)
- Modify: `apps/paas-engine/internal/adapter/repository/model.go` (追加 DynamicConfigModel)
- Modify: `apps/paas-engine/internal/adapter/repository/db.go` (AutoMigrate 追加)
- Create: `apps/paas-engine/internal/adapter/repository/dynamic_config_repo.go`

- [ ] **Step 1: 创建 domain 模型**

`apps/paas-engine/internal/domain/dynamic_config.go`:
```go
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
```

- [ ] **Step 2: 追加 domain 错误**

在 `apps/paas-engine/internal/domain/errors.go` 的 var 块中追加：
```go
ErrDynamicConfigNotFound = fmt.Errorf("dynamic config %w", ErrNotFound)
```

- [ ] **Step 3: 追加 Port 接口**

在 `apps/paas-engine/internal/port/repository.go` 末尾追加：
```go
type DynamicConfigRepository interface {
	Upsert(ctx context.Context, config *domain.DynamicConfig) error
	FindByKeyAndLane(ctx context.Context, key, lane string) (*domain.DynamicConfig, error)
	FindByLane(ctx context.Context, lane string) ([]*domain.DynamicConfig, error)
	FindAll(ctx context.Context) ([]*domain.DynamicConfig, error)
	DeleteByKeyAndLane(ctx context.Context, key, lane string) error
	DeleteByKey(ctx context.Context, key string) error
}
```

- [ ] **Step 4: 追加 DB Model**

在 `apps/paas-engine/internal/adapter/repository/model.go` 末尾追加：
```go
// DynamicConfigModel 是 DynamicConfig 的数据库持久化模型。
type DynamicConfigModel struct {
	Key       string    `gorm:"primaryKey"`
	Lane      string    `gorm:"primaryKey;default:prod"`
	Value     string    `gorm:"not null"`
	UpdatedAt time.Time
}

func (DynamicConfigModel) TableName() string { return "dynamic_configs" }
```

- [ ] **Step 5: 追加 AutoMigrate**

在 `apps/paas-engine/internal/adapter/repository/db.go` 的 `db.AutoMigrate()` 调用中，追加 `&DynamicConfigModel{}`（在 `&DbMutationModel{}` 之后）。

- [ ] **Step 6: 实现 Repo**

`apps/paas-engine/internal/adapter/repository/dynamic_config_repo.go`:
```go
package repository

import (
	"context"
	"errors"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
	"gorm.io/gorm"
	"gorm.io/gorm/clause"
)

var _ port.DynamicConfigRepository = (*DynamicConfigRepo)(nil)

type DynamicConfigRepo struct {
	db *gorm.DB
}

func NewDynamicConfigRepo(db *gorm.DB) *DynamicConfigRepo {
	return &DynamicConfigRepo{db: db}
}

func (r *DynamicConfigRepo) Upsert(ctx context.Context, config *domain.DynamicConfig) error {
	m := DynamicConfigModel{
		Key:       config.Key,
		Lane:      config.Lane,
		Value:     config.Value,
		UpdatedAt: config.UpdatedAt,
	}
	return r.db.WithContext(ctx).Clauses(clause.OnConflict{
		Columns:   []clause.Column{{Name: "key"}, {Name: "lane"}},
		DoUpdates: clause.AssignmentColumns([]string{"value", "updated_at"}),
	}).Create(&m).Error
}

func (r *DynamicConfigRepo) FindByKeyAndLane(ctx context.Context, key, lane string) (*domain.DynamicConfig, error) {
	var m DynamicConfigModel
	result := r.db.WithContext(ctx).First(&m, "key = ? AND lane = ?", key, lane)
	if result.Error != nil {
		if errors.Is(result.Error, gorm.ErrRecordNotFound) {
			return nil, domain.ErrDynamicConfigNotFound
		}
		return nil, result.Error
	}
	return modelToDynamicConfig(&m), nil
}

func (r *DynamicConfigRepo) FindByLane(ctx context.Context, lane string) ([]*domain.DynamicConfig, error) {
	var models []DynamicConfigModel
	if err := r.db.WithContext(ctx).Where("lane = ?", lane).Find(&models).Error; err != nil {
		return nil, err
	}
	return modelsToDynamicConfigs(models), nil
}

func (r *DynamicConfigRepo) FindAll(ctx context.Context) ([]*domain.DynamicConfig, error) {
	var models []DynamicConfigModel
	if err := r.db.WithContext(ctx).Order("key, lane").Find(&models).Error; err != nil {
		return nil, err
	}
	return modelsToDynamicConfigs(models), nil
}

func (r *DynamicConfigRepo) DeleteByKeyAndLane(ctx context.Context, key, lane string) error {
	result := r.db.WithContext(ctx).Delete(&DynamicConfigModel{}, "key = ? AND lane = ?", key, lane)
	if result.RowsAffected == 0 {
		return domain.ErrDynamicConfigNotFound
	}
	return result.Error
}

func (r *DynamicConfigRepo) DeleteByKey(ctx context.Context, key string) error {
	result := r.db.WithContext(ctx).Delete(&DynamicConfigModel{}, "key = ?", key)
	if result.RowsAffected == 0 {
		return domain.ErrDynamicConfigNotFound
	}
	return result.Error
}

func modelToDynamicConfig(m *DynamicConfigModel) *domain.DynamicConfig {
	return &domain.DynamicConfig{
		Key:       m.Key,
		Lane:      m.Lane,
		Value:     m.Value,
		UpdatedAt: m.UpdatedAt,
	}
}

func modelsToDynamicConfigs(models []DynamicConfigModel) []*domain.DynamicConfig {
	configs := make([]*domain.DynamicConfig, 0, len(models))
	for i := range models {
		configs = append(configs, modelToDynamicConfig(&models[i]))
	}
	return configs
}
```

- [ ] **Step 7: 编译验证**

Run: `cd apps/paas-engine && go build ./...`
Expected: 编译通过

- [ ] **Step 8: Commit**

```bash
git add apps/paas-engine/internal/domain/dynamic_config.go \
       apps/paas-engine/internal/domain/errors.go \
       apps/paas-engine/internal/port/repository.go \
       apps/paas-engine/internal/adapter/repository/model.go \
       apps/paas-engine/internal/adapter/repository/db.go \
       apps/paas-engine/internal/adapter/repository/dynamic_config_repo.go
git commit -m "feat(paas-engine): add dynamic config data layer"
```

---

### Task 2: paas-engine Service 层 + 测试

**Files:**
- Create: `apps/paas-engine/internal/service/dynamic_config_service.go`
- Create: `apps/paas-engine/internal/service/dynamic_config_service_test.go`

- [ ] **Step 1: 写测试 — stub repo + Resolve 测试**

`apps/paas-engine/internal/service/dynamic_config_service_test.go`:
```go
package service

import (
	"context"
	"testing"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
)

// --- stub for DynamicConfigRepository ---

type stubDynamicConfigRepo struct {
	configs map[string]*domain.DynamicConfig // key = "key|lane"
}

func newStubDynamicConfigRepo() *stubDynamicConfigRepo {
	return &stubDynamicConfigRepo{configs: make(map[string]*domain.DynamicConfig)}
}

func compositeKey(key, lane string) string { return key + "|" + lane }

func (r *stubDynamicConfigRepo) Upsert(_ context.Context, config *domain.DynamicConfig) error {
	r.configs[compositeKey(config.Key, config.Lane)] = config
	return nil
}

func (r *stubDynamicConfigRepo) FindByKeyAndLane(_ context.Context, key, lane string) (*domain.DynamicConfig, error) {
	c, ok := r.configs[compositeKey(key, lane)]
	if !ok {
		return nil, domain.ErrDynamicConfigNotFound
	}
	return c, nil
}

func (r *stubDynamicConfigRepo) FindByLane(_ context.Context, lane string) ([]*domain.DynamicConfig, error) {
	var result []*domain.DynamicConfig
	for _, c := range r.configs {
		if c.Lane == lane {
			result = append(result, c)
		}
	}
	return result, nil
}

func (r *stubDynamicConfigRepo) FindAll(_ context.Context) ([]*domain.DynamicConfig, error) {
	var result []*domain.DynamicConfig
	for _, c := range r.configs {
		result = append(result, c)
	}
	return result, nil
}

func (r *stubDynamicConfigRepo) DeleteByKeyAndLane(_ context.Context, key, lane string) error {
	ck := compositeKey(key, lane)
	if _, ok := r.configs[ck]; !ok {
		return domain.ErrDynamicConfigNotFound
	}
	delete(r.configs, ck)
	return nil
}

func (r *stubDynamicConfigRepo) DeleteByKey(_ context.Context, key string) error {
	found := false
	for ck, c := range r.configs {
		if c.Key == key {
			delete(r.configs, ck)
			found = true
		}
	}
	if !found {
		return domain.ErrDynamicConfigNotFound
	}
	return nil
}

// --- tests ---

func TestResolve_ProdBaseline(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	repo.configs[compositeKey("model", "prod")] = &domain.DynamicConfig{
		Key: "model", Lane: "prod", Value: "gemini", UpdatedAt: time.Now(),
	}
	svc := NewDynamicConfigService(repo)

	result, err := svc.Resolve(context.Background(), "prod")
	if err != nil {
		t.Fatal(err)
	}
	if result.Configs["model"].Value != "gemini" {
		t.Errorf("expected gemini, got %s", result.Configs["model"].Value)
	}
	if result.Configs["model"].Lane != "prod" {
		t.Errorf("expected lane=prod, got %s", result.Configs["model"].Lane)
	}
}

func TestResolve_LaneOverride(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	repo.configs[compositeKey("model", "prod")] = &domain.DynamicConfig{
		Key: "model", Lane: "prod", Value: "gemini", UpdatedAt: time.Now(),
	}
	repo.configs[compositeKey("model", "dev")] = &domain.DynamicConfig{
		Key: "model", Lane: "dev", Value: "gpt-4o", UpdatedAt: time.Now(),
	}
	repo.configs[compositeKey("threshold", "prod")] = &domain.DynamicConfig{
		Key: "threshold", Lane: "prod", Value: "0.7", UpdatedAt: time.Now(),
	}
	svc := NewDynamicConfigService(repo)

	result, err := svc.Resolve(context.Background(), "dev")
	if err != nil {
		t.Fatal(err)
	}
	// model should be overridden by dev
	if result.Configs["model"].Value != "gpt-4o" {
		t.Errorf("expected gpt-4o, got %s", result.Configs["model"].Value)
	}
	if result.Configs["model"].Lane != "dev" {
		t.Errorf("expected lane=dev, got %s", result.Configs["model"].Lane)
	}
	// threshold should fallback to prod
	if result.Configs["threshold"].Value != "0.7" {
		t.Errorf("expected 0.7, got %s", result.Configs["threshold"].Value)
	}
	if result.Configs["threshold"].Lane != "prod" {
		t.Errorf("expected lane=prod, got %s", result.Configs["threshold"].Lane)
	}
}

func TestResolve_EmptyLaneFallbackProd(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	repo.configs[compositeKey("model", "prod")] = &domain.DynamicConfig{
		Key: "model", Lane: "prod", Value: "gemini", UpdatedAt: time.Now(),
	}
	svc := NewDynamicConfigService(repo)

	result, err := svc.Resolve(context.Background(), "")
	if err != nil {
		t.Fatal(err)
	}
	if result.Configs["model"].Value != "gemini" {
		t.Errorf("expected gemini, got %s", result.Configs["model"].Value)
	}
}

func TestSetAndDelete(t *testing.T) {
	repo := newStubDynamicConfigRepo()
	svc := NewDynamicConfigService(repo)

	// Set
	err := svc.Set(context.Background(), "model", SetDynamicConfigRequest{Lane: "prod", Value: "gemini"})
	if err != nil {
		t.Fatal(err)
	}

	result, err := svc.Resolve(context.Background(), "prod")
	if err != nil {
		t.Fatal(err)
	}
	if result.Configs["model"].Value != "gemini" {
		t.Errorf("expected gemini, got %s", result.Configs["model"].Value)
	}

	// Delete
	err = svc.Delete(context.Background(), "model", "prod")
	if err != nil {
		t.Fatal(err)
	}

	result, err = svc.Resolve(context.Background(), "prod")
	if err != nil {
		t.Fatal(err)
	}
	if _, exists := result.Configs["model"]; exists {
		t.Error("expected model to be deleted")
	}
}
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd apps/paas-engine && go test ./internal/service/ -run TestResolve -v`
Expected: 编译失败 — `NewDynamicConfigService` 未定义

- [ ] **Step 3: 实现 Service**

`apps/paas-engine/internal/service/dynamic_config_service.go`:
```go
package service

import (
	"context"
	"time"

	"github.com/chiwei-platform/paas-engine/internal/domain"
	"github.com/chiwei-platform/paas-engine/internal/port"
)

type DynamicConfigService struct {
	repo port.DynamicConfigRepository
}

func NewDynamicConfigService(repo port.DynamicConfigRepository) *DynamicConfigService {
	return &DynamicConfigService{repo: repo}
}

// ResolvedEntry 是解析后的单条配置，带来源 lane 标注。
type ResolvedEntry struct {
	Value string `json:"value"`
	Lane  string `json:"lane"`
}

// ResolvedConfig 是解析后的全量配置快照。
type ResolvedConfig struct {
	Configs    map[string]ResolvedEntry `json:"configs"`
	ResolvedAt time.Time               `json:"resolved_at"`
}

// Resolve 返回指定泳道的合并配置（lane 覆盖 + prod 补缺）。
func (s *DynamicConfigService) Resolve(ctx context.Context, lane string) (*ResolvedConfig, error) {
	if lane == "" {
		lane = "prod"
	}

	// 先取 prod 基线
	prodConfigs, err := s.repo.FindByLane(ctx, "prod")
	if err != nil {
		return nil, err
	}

	result := make(map[string]ResolvedEntry, len(prodConfigs))
	for _, c := range prodConfigs {
		result[c.Key] = ResolvedEntry{Value: c.Value, Lane: "prod"}
	}

	// 非 prod 泳道：用该泳道的值覆盖
	if lane != "prod" {
		laneConfigs, err := s.repo.FindByLane(ctx, lane)
		if err != nil {
			return nil, err
		}
		for _, c := range laneConfigs {
			result[c.Key] = ResolvedEntry{Value: c.Value, Lane: lane}
		}
	}

	return &ResolvedConfig{
		Configs:    result,
		ResolvedAt: time.Now(),
	}, nil
}

// List 返回所有配置（可选按 lane 筛选）。
func (s *DynamicConfigService) List(ctx context.Context, lane string) ([]*domain.DynamicConfig, error) {
	if lane != "" {
		return s.repo.FindByLane(ctx, lane)
	}
	return s.repo.FindAll(ctx)
}

// SetDynamicConfigRequest 是设置配置的请求体。
type SetDynamicConfigRequest struct {
	Lane  string `json:"lane"`
	Value string `json:"value"`
}

// Set 设置一条配置（upsert 语义）。
func (s *DynamicConfigService) Set(ctx context.Context, key string, req SetDynamicConfigRequest) error {
	if key == "" {
		return domain.ErrInvalidInput
	}
	lane := req.Lane
	if lane == "" {
		lane = "prod"
	}
	return s.repo.Upsert(ctx, &domain.DynamicConfig{
		Key:       key,
		Lane:      lane,
		Value:     req.Value,
		UpdatedAt: time.Now(),
	})
}

// Delete 删除配置。lane 为空则删除所有 lane 的该 key。
func (s *DynamicConfigService) Delete(ctx context.Context, key, lane string) error {
	if lane != "" {
		return s.repo.DeleteByKeyAndLane(ctx, key, lane)
	}
	return s.repo.DeleteByKey(ctx, key)
}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd apps/paas-engine && go test ./internal/service/ -run "TestResolve|TestSetAndDelete" -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add apps/paas-engine/internal/service/dynamic_config_service.go \
       apps/paas-engine/internal/service/dynamic_config_service_test.go
git commit -m "feat(paas-engine): add dynamic config service with tests"
```

---

### Task 3: paas-engine HTTP 层 + 路由 + 接线

**Files:**
- Create: `apps/paas-engine/internal/adapter/http/dynamic_config_handler.go`
- Modify: `apps/paas-engine/internal/adapter/http/router.go` (追加路由)
- Modify: `apps/paas-engine/cmd/paas-engine/main.go` (接线)

- [ ] **Step 1: 实现 Handler**

`apps/paas-engine/internal/adapter/http/dynamic_config_handler.go`:
```go
package http

import (
	"encoding/json"
	"net/http"

	"github.com/chiwei-platform/paas-engine/internal/service"
	"github.com/go-chi/chi/v5"
)

type DynamicConfigHandler struct {
	svc *service.DynamicConfigService
}

func NewDynamicConfigHandler(svc *service.DynamicConfigService) *DynamicConfigHandler {
	return &DynamicConfigHandler{svc: svc}
}

// Resolve 返回合并后的全量配置快照（供 SDK 调用，无 auth）。
func (h *DynamicConfigHandler) Resolve(w http.ResponseWriter, r *http.Request) {
	lane := r.URL.Query().Get("lane")
	result, err := h.svc.Resolve(r.Context(), lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, result)
}

// List 列出所有配置（支持 ?lane= 筛选）。
func (h *DynamicConfigHandler) List(w http.ResponseWriter, r *http.Request) {
	lane := r.URL.Query().Get("lane")
	configs, err := h.svc.List(r.Context(), lane)
	if err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, configs)
}

// Set 设置配置（upsert 语义）。
func (h *DynamicConfigHandler) Set(w http.ResponseWriter, r *http.Request) {
	key := chi.URLParam(r, "key")
	var req service.SetDynamicConfigRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, err)
		return
	}
	if err := h.svc.Set(r.Context(), key, req); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"key": key, "lane": req.Lane, "status": "ok"})
}

// Delete 删除配置。?lane= 指定 lane 则只删该 lane 的覆盖，不传则删所有。
func (h *DynamicConfigHandler) Delete(w http.ResponseWriter, r *http.Request) {
	key := chi.URLParam(r, "key")
	lane := r.URL.Query().Get("lane")
	if err := h.svc.Delete(r.Context(), key, lane); err != nil {
		writeError(w, err)
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"deleted": key})
}
```

- [ ] **Step 2: 注册路由**

在 `apps/paas-engine/internal/adapter/http/router.go` 中：

1. `NewRouter` 函数签名追加参数 `dynamicConfigH *DynamicConfigHandler`（加在 `configBundleH` 之后，`apiToken` 之前）

2. 在 `r.Route("/api/paas", ...)` 块**之前**追加内部读取端点（无 auth）：
```go
// Dynamic Config — internal read endpoint (no auth, for SDK)
r.Get("/internal/dynamic-config/resolved", dynamicConfigH.Resolve)
```

3. 在 `r.Route("/api/paas", ...)` 块**内部**、`// Config Bundles` 之后追加管理端点：
```go
// Dynamic Config (management)
r.Route("/dynamic-config", func(r chi.Router) {
	r.Get("/", dynamicConfigH.List)
	r.Get("/resolved", dynamicConfigH.Resolve)
	r.Put("/{key}", dynamicConfigH.Set)
	r.Delete("/{key}", dynamicConfigH.Delete)
})
```

注意：`Resolve` 注册了两次 —— `/internal/` 路径供 SDK 无 auth 调用，`/api/paas/` 路径供 Dashboard 带 auth 调用。同一个 handler 方法。

- [ ] **Step 3: main.go 接线**

在 `apps/paas-engine/cmd/paas-engine/main.go` 中：

1. 在 `configBundleRepo := ...` 之后追加：
```go
dynamicConfigRepo := repository.NewDynamicConfigRepo(db)
```

2. 在 `configBundleSvc := ...` 之后追加：
```go
dynamicConfigSvc := service.NewDynamicConfigService(dynamicConfigRepo)
```

3. 修改 `httpadapter.NewRouter(...)` 调用，在 `httpadapter.NewConfigBundleHandler(configBundleSvc)` 之后追加：
```go
httpadapter.NewDynamicConfigHandler(dynamicConfigSvc),
```

- [ ] **Step 4: 编译验证**

Run: `cd apps/paas-engine && go build ./...`
Expected: 编译通过

- [ ] **Step 5: 全量测试**

Run: `cd apps/paas-engine && go test ./... -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/paas-engine/internal/adapter/http/dynamic_config_handler.go \
       apps/paas-engine/internal/adapter/http/router.go \
       apps/paas-engine/cmd/paas-engine/main.go
git commit -m "feat(paas-engine): add dynamic config HTTP endpoints and wiring"
```

---

### Task 4: Python SDK

**Files:**
- Create: `packages/py-shared/inner_shared/dynamic_config.py`
- Modify: `packages/py-shared/inner_shared/__init__.py` (追加导出)

- [ ] **Step 1: 实现 SDK**

`packages/py-shared/inner_shared/dynamic_config.py`:
```python
"""
DynamicConfig — 运行时动态配置 SDK (Python)

用法::

    from inner_shared.dynamic_config import dynamic_config

    model = dynamic_config.get("default_model", default="gemini")
    threshold = dynamic_config.get_float("proactive_threshold", default=0.7)
    enabled = dynamic_config.get_bool("feature_x_enabled", default=False)
    count = dynamic_config.get_int("max_retry", default=3)
"""

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL = 10  # seconds


class DynamicConfig:
    """
    运行时动态配置读取器。

    从 paas-engine 拉取全量配置快照，按泳道缓存 10s。
    lane 通过 lane_provider 自动获取（从 context），取不到则为 "prod"。
    """

    def __init__(
        self,
        paas_engine_url: str = "http://paas-engine:8080",
        lane_provider: Callable[[], str | None] | None = None,
    ):
        self._paas_engine_url = paas_engine_url.rstrip("/")
        self._lane_provider = lane_provider
        self._cache: dict[str, tuple[dict[str, dict[str, str]], float]] = {}
        self._lock = threading.Lock()

    def _get_lane(self) -> str:
        if self._lane_provider:
            lane = self._lane_provider()
            if lane:
                return lane
        return "prod"

    def _fetch_snapshot(self, lane: str) -> dict[str, dict[str, str]]:
        """从 paas-engine 拉取合并后的配置快照。"""
        url = f"{self._paas_engine_url}/internal/dynamic-config/resolved"
        try:
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(url, params={"lane": lane})
                if resp.status_code == 200:
                    body = resp.json()
                    data = body.get("data", body)
                    return data.get("configs", {})
                logger.warning(
                    "[DynamicConfig] paas-engine responded %d", resp.status_code
                )
        except Exception as e:
            logger.warning("[DynamicConfig] failed to fetch config: %s", e)
        return {}

    def _get_snapshot(self, lane: str) -> dict[str, dict[str, str]]:
        """获取缓存的快照，过期则刷新。"""
        now = time.monotonic()
        with self._lock:
            if lane in self._cache:
                snapshot, expire_at = self._cache[lane]
                if now < expire_at:
                    return snapshot

        # 缓存过期或不存在，拉取新数据（lock 外执行网络请求）
        snapshot = self._fetch_snapshot(lane)
        with self._lock:
            self._cache[lane] = (snapshot, now + _CACHE_TTL)
        return snapshot

    def get(self, key: str, *, default: str = "") -> str:
        """获取配置值（字符串），不存在则返回 default。"""
        lane = self._get_lane()
        snapshot = self._get_snapshot(lane)
        entry = snapshot.get(key)
        if entry is None:
            return default
        return entry.get("value", default)

    def get_int(self, key: str, *, default: int = 0) -> int:
        """获取配置值（整数），转换失败返回 default。"""
        raw = self.get(key, default="")
        if raw == "":
            return default
        try:
            return int(raw)
        except (ValueError, TypeError):
            return default

    def get_float(self, key: str, *, default: float = 0.0) -> float:
        """获取配置值（浮点），转换失败返回 default。"""
        raw = self.get(key, default="")
        if raw == "":
            return default
        try:
            return float(raw)
        except (ValueError, TypeError):
            return default

    def get_bool(self, key: str, *, default: bool = False) -> bool:
        """获取配置值（布尔），true/1/yes 为 True，其他为 False。"""
        raw = self.get(key, default="")
        if raw == "":
            return default
        return raw.lower() in ("true", "1", "yes")
```

- [ ] **Step 2: 追加导出**

在 `packages/py-shared/inner_shared/__init__.py` 中：

1. 在 `from .lane_router import LaneRouter` 之后追加：
```python
# DynamicConfig
from .dynamic_config import DynamicConfig
```

2. 在 `__all__` 列表中追加：
```python
    # DynamicConfig
    "DynamicConfig",
```

- [ ] **Step 3: 语法验证**

Run: `cd packages/py-shared && python -c "from inner_shared.dynamic_config import DynamicConfig; print('OK')"`
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add packages/py-shared/inner_shared/dynamic_config.py \
       packages/py-shared/inner_shared/__init__.py
git commit -m "feat(py-shared): add DynamicConfig SDK"
```

---

### Task 5: TypeScript SDK

**Files:**
- Create: `packages/ts-shared/src/dynamic-config/index.ts`
- Modify: `packages/ts-shared/src/index.ts` (追加导出)

- [ ] **Step 1: 实现 SDK**

`packages/ts-shared/src/dynamic-config/index.ts`:
```typescript
/**
 * DynamicConfig — 运行时动态配置 SDK (TypeScript)
 *
 * 用法:
 *   import { DynamicConfig } from 'ts-shared/dynamic-config'
 *
 *   const config = new DynamicConfig({ laneProvider: () => context.get('lane') })
 *   const model = config.get("default_model", "gemini")
 *   const threshold = config.getFloat("proactive_threshold", 0.7)
 */

import { context } from '../middleware/context';

const CACHE_TTL = 10_000; // 10 seconds in ms

interface ConfigEntry {
    value: string;
    lane: string;
}

interface ResolvedResponse {
    data?: {
        configs: Record<string, ConfigEntry>;
        resolved_at: string;
    };
    configs?: Record<string, ConfigEntry>;
}

interface CacheEntry {
    snapshot: Record<string, ConfigEntry>;
    expireAt: number;
}

export interface DynamicConfigOptions {
    paasEngineUrl?: string;
    laneProvider?: () => string | undefined;
}

export class DynamicConfig {
    private paasEngineUrl: string;
    private laneProvider: () => string | undefined;
    private cache: Map<string, CacheEntry> = new Map();

    constructor(options: DynamicConfigOptions = {}) {
        this.paasEngineUrl = (options.paasEngineUrl || 'http://paas-engine:8080').replace(/\/+$/, '');
        this.laneProvider = options.laneProvider || (() => context.get<string>('lane'));
    }

    private getLane(): string {
        const lane = this.laneProvider();
        return lane || 'prod';
    }

    private async fetchSnapshot(lane: string): Promise<Record<string, ConfigEntry>> {
        try {
            const url = `${this.paasEngineUrl}/internal/dynamic-config/resolved?lane=${encodeURIComponent(lane)}`;
            const resp = await fetch(url, { signal: AbortSignal.timeout(5000) });
            if (resp.ok) {
                const body: ResolvedResponse = await resp.json();
                const data = body.data ?? body;
                return data.configs ?? {};
            }
            console.warn(`[DynamicConfig] paas-engine responded ${resp.status}`);
        } catch (err) {
            console.warn('[DynamicConfig] failed to fetch config:', err);
        }
        return {};
    }

    private async getSnapshot(lane: string): Promise<Record<string, ConfigEntry>> {
        const now = Date.now();
        const cached = this.cache.get(lane);
        if (cached && now < cached.expireAt) {
            return cached.snapshot;
        }
        const snapshot = await this.fetchSnapshot(lane);
        this.cache.set(lane, { snapshot, expireAt: now + CACHE_TTL });
        return snapshot;
    }

    async get(key: string, defaultValue: string = ''): Promise<string> {
        const lane = this.getLane();
        const snapshot = await this.getSnapshot(lane);
        return snapshot[key]?.value ?? defaultValue;
    }

    async getInt(key: string, defaultValue: number = 0): Promise<number> {
        const raw = await this.get(key);
        if (raw === '') return defaultValue;
        const n = parseInt(raw, 10);
        return isNaN(n) ? defaultValue : n;
    }

    async getFloat(key: string, defaultValue: number = 0): Promise<number> {
        const raw = await this.get(key);
        if (raw === '') return defaultValue;
        const n = parseFloat(raw);
        return isNaN(n) ? defaultValue : n;
    }

    async getBool(key: string, defaultValue: boolean = false): Promise<boolean> {
        const raw = await this.get(key);
        if (raw === '') return defaultValue;
        return ['true', '1', 'yes'].includes(raw.toLowerCase());
    }
}
```

- [ ] **Step 2: 追加导出**

在 `packages/ts-shared/src/index.ts` 末尾追加：
```typescript

// DynamicConfig exports
export type { DynamicConfigOptions } from './dynamic-config';
export { DynamicConfig } from './dynamic-config';
```

- [ ] **Step 3: 类型检查**

Run: `cd packages/ts-shared && npx tsc --noEmit 2>&1 | head -20`
Expected: 无错误（或项目原有的错误，不应有新增）

- [ ] **Step 4: Commit**

```bash
git add packages/ts-shared/src/dynamic-config/index.ts \
       packages/ts-shared/src/index.ts
git commit -m "feat(ts-shared): add DynamicConfig SDK"
```

---

### Task 6: Dashboard — 后端代理 + 前端页面

**Files:**
- Modify: `apps/monitor-dashboard/src/paas-client.ts` (追加 put 方法)
- Create: `apps/monitor-dashboard/src/routes/dynamic-config.ts`
- Modify: `apps/monitor-dashboard/src/index.ts` (注册路由)
- Create: `apps/monitor-dashboard-web/src/pages/DynamicConfig.tsx`
- Modify: `apps/monitor-dashboard-web/src/App.tsx` (追加菜单和路由)

- [ ] **Step 1: paasClient 追加 put 方法**

在 `apps/monitor-dashboard/src/paas-client.ts` 的 `createClient` 函数中，在 `del` 方法之后追加：
```typescript
    async put(path: string, body?: unknown, extraHeaders?: Record<string, string>) {
      const { baseURL, headers } = configFn();
      const url = laneAwareUrl(baseURL, extraHeaders?.['x-lane']);
      const config: AxiosRequestConfig = {
        headers: { ...headers, 'Content-Type': 'application/json', ...extraHeaders },
        timeout: TIMEOUT,
      };
      const res = await axios.put(`${url}${path}`, body, config);
      return unwrap(res.data);
    },
```

- [ ] **Step 2: 创建后端代理路由**

`apps/monitor-dashboard/src/routes/dynamic-config.ts`:
```typescript
import { Hono } from 'hono';
import { paasClient } from '../paas-client';

const app = new Hono();

/** GET /api/dynamic-config — 列出所有配置 */
app.get('/api/dynamic-config', async (c) => {
  const lane = c.req.query('lane');
  const params: Record<string, string> = {};
  if (lane) params.lane = lane;
  const data = await paasClient.get('/api/paas/dynamic-config/', params);
  return c.json(data);
});

/** GET /api/dynamic-config/resolved — 解析后的配置快照 */
app.get('/api/dynamic-config/resolved', async (c) => {
  const lane = c.req.query('lane') || 'prod';
  const data = await paasClient.get('/api/paas/dynamic-config/resolved', { lane });
  return c.json(data);
});

/** PUT /api/dynamic-config/:key — 设置配置 */
app.put('/api/dynamic-config/:key', async (c) => {
  const key = c.req.param('key');
  const body = await c.req.json();
  const data = await paasClient.put(`/api/paas/dynamic-config/${encodeURIComponent(key)}`, body);
  return c.json(data);
});

/** DELETE /api/dynamic-config/:key — 删除配置 */
app.delete('/api/dynamic-config/:key', async (c) => {
  const key = c.req.param('key');
  const lane = c.req.query('lane');
  const params: Record<string, string> = {};
  if (lane) params.lane = lane;
  const data = await paasClient.del(`/api/paas/dynamic-config/${encodeURIComponent(key)}`, params);
  return c.json(data);
});

export default app;
```

- [ ] **Step 3: 注册路由**

在 `apps/monitor-dashboard/src/index.ts` 中：

1. 追加 import：
```typescript
import dynamicConfigRoutes from './routes/dynamic-config';
```

2. 在 `dashboard.route('/', activityRoutes);` 之后追加：
```typescript
dashboard.route('/', dynamicConfigRoutes);
```

- [ ] **Step 4: 创建前端页面**

`apps/monitor-dashboard-web/src/pages/DynamicConfig.tsx`:
```tsx
import { useEffect, useState, useCallback } from 'react';
import {
  Button,
  Form,
  Input,
  Modal,
  Popconfirm,
  Select,
  Space,
  Table,
  Tag,
  Typography,
  message,
} from 'antd';
import type { ColumnsType } from 'antd/es/table';
import { PlusOutlined, EditOutlined, DeleteOutlined, UndoOutlined } from '@ant-design/icons';
import { api } from '../api/client';

const { Text } = Typography;

interface ConfigEntry {
  value: string;
  lane: string;
}

interface ResolvedData {
  configs: Record<string, ConfigEntry>;
  resolved_at: string;
}

interface RawConfig {
  key: string;
  lane: string;
  value: string;
  updated_at: string;
}

export default function DynamicConfig() {
  const [lanes, setLanes] = useState<string[]>(['prod']);
  const [selectedLane, setSelectedLane] = useState('prod');
  const [resolved, setResolved] = useState<Record<string, ConfigEntry>>({});
  const [loading, setLoading] = useState(false);
  const [modalOpen, setModalOpen] = useState(false);
  const [editingKey, setEditingKey] = useState<string | null>(null);
  const [form] = Form.useForm();

  const fetchResolved = useCallback(async (lane: string) => {
    setLoading(true);
    try {
      const { data } = await api.get('/dynamic-config/resolved', { params: { lane } });
      setResolved(data?.configs || data?.data?.configs || {});
    } finally {
      setLoading(false);
    }
  }, []);

  const fetchLanes = useCallback(async () => {
    try {
      const { data } = await api.get('/dynamic-config');
      const raw: RawConfig[] = Array.isArray(data) ? data : (data?.data || []);
      const laneSet = new Set<string>(['prod']);
      raw.forEach((c: RawConfig) => laneSet.add(c.lane));
      setLanes(Array.from(laneSet).sort());
    } catch {
      // ignore
    }
  }, []);

  useEffect(() => {
    fetchLanes();
  }, [fetchLanes]);

  useEffect(() => {
    fetchResolved(selectedLane);
  }, [selectedLane, fetchResolved]);

  const handleSave = async () => {
    try {
      const values = await form.validateFields();
      await api.put(`/dynamic-config/${encodeURIComponent(values.key)}`, {
        lane: selectedLane,
        value: values.value,
      });
      message.success('已保存');
      setModalOpen(false);
      form.resetFields();
      setEditingKey(null);
      fetchResolved(selectedLane);
      fetchLanes();
    } catch {
      // form validation error
    }
  };

  const handleDelete = async (key: string) => {
    if (selectedLane === 'prod') {
      await api.delete(`/dynamic-config/${encodeURIComponent(key)}`);
      message.success('已删除');
    } else {
      await api.delete(`/dynamic-config/${encodeURIComponent(key)}`, {
        params: { lane: selectedLane },
      });
      message.success('已恢复到 prod');
    }
    fetchResolved(selectedLane);
    fetchLanes();
  };

  const openEdit = (key: string, value: string) => {
    setEditingKey(key);
    form.setFieldsValue({ key, value });
    setModalOpen(true);
  };

  const openCreate = () => {
    setEditingKey(null);
    form.resetFields();
    setModalOpen(true);
  };

  const dataSource = Object.entries(resolved)
    .map(([key, entry]) => ({ key, ...entry }))
    .sort((a, b) => a.key.localeCompare(b.key));

  const columns: ColumnsType<{ key: string; value: string; lane: string }> = [
    {
      title: 'Key',
      dataIndex: 'key',
      width: 280,
      render: (text: string) => <Text code>{text}</Text>,
    },
    {
      title: 'Value',
      dataIndex: 'value',
      ellipsis: true,
    },
    {
      title: '来源',
      dataIndex: 'lane',
      width: 120,
      render: (lane: string) => (
        <Tag color={lane === selectedLane && lane !== 'prod' ? 'blue' : 'default'}>
          {lane === selectedLane && lane !== 'prod' ? '本泳道' : 'prod'}
        </Tag>
      ),
    },
    {
      title: '操作',
      width: 160,
      render: (_: unknown, record: { key: string; value: string; lane: string }) => (
        <Space>
          <Button
            type="link"
            size="small"
            icon={<EditOutlined />}
            onClick={() => openEdit(record.key, record.value)}
          >
            编辑
          </Button>
          {selectedLane !== 'prod' && record.lane === selectedLane ? (
            <Popconfirm title="恢复到 prod 值？" onConfirm={() => handleDelete(record.key)}>
              <Button type="link" size="small" icon={<UndoOutlined />} danger>
                恢复
              </Button>
            </Popconfirm>
          ) : selectedLane === 'prod' ? (
            <Popconfirm title="删除此配置？" onConfirm={() => handleDelete(record.key)}>
              <Button type="link" size="small" icon={<DeleteOutlined />} danger>
                删除
              </Button>
            </Popconfirm>
          ) : null}
        </Space>
      ),
    },
  ];

  return (
    <div>
      <div style={{ marginBottom: 16, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <Space>
          <Text strong>泳道：</Text>
          <Select
            value={selectedLane}
            onChange={setSelectedLane}
            style={{ width: 160 }}
            options={lanes.map((l) => ({ label: l, value: l }))}
          />
        </Space>
        <Button type="primary" icon={<PlusOutlined />} onClick={openCreate}>
          新增配置
        </Button>
      </div>

      <Table
        dataSource={dataSource}
        columns={columns}
        loading={loading}
        pagination={false}
        size="middle"
        rowKey="key"
      />

      <Modal
        title={editingKey ? `编辑 ${editingKey}` : '新增配置'}
        open={modalOpen}
        onOk={handleSave}
        onCancel={() => { setModalOpen(false); form.resetFields(); setEditingKey(null); }}
        okText="保存"
        cancelText="取消"
      >
        <Form form={form} layout="vertical">
          <Form.Item
            name="key"
            label="Key"
            rules={[{ required: true, message: '请输入 key' }]}
          >
            <Input disabled={!!editingKey} placeholder="如 default_model" />
          </Form.Item>
          <Form.Item
            name="value"
            label="Value"
            rules={[{ required: true, message: '请输入 value' }]}
          >
            <Input.TextArea rows={3} placeholder="配置值" />
          </Form.Item>
          <Form.Item label="泳道">
            <Tag>{selectedLane}</Tag>
            {selectedLane !== 'prod' && (
              <Text type="secondary" style={{ marginLeft: 8 }}>
                此值仅在 {selectedLane} 泳道生效
              </Text>
            )}
          </Form.Item>
        </Form>
      </Modal>
    </div>
  );
}
```

- [ ] **Step 5: 注册前端路由和菜单**

在 `apps/monitor-dashboard-web/src/App.tsx` 中：

1. 追加 import icon（在现有 icon import 行追加）：
```typescript
import { SettingOutlined } from '@ant-design/icons';
```

2. 追加 lazy import（在现有 lazy import 行之后）：
```typescript
const DynamicConfig = lazy(() => import('./pages/DynamicConfig'));
```

3. 在 `menuItems` 数组中追加（在 `{ key: '/model-mappings', ... }` 之后）：
```typescript
{ key: '/dynamic-config', icon: <SettingOutlined />, label: '动态配置' },
```

4. 在 `<Routes>` 中追加（在 `<Route path="/model-mappings" ...>` 之后）：
```tsx
<Route path="/dynamic-config" element={<DynamicConfig />} />
```

- [ ] **Step 6: Commit**

```bash
git add apps/monitor-dashboard/src/paas-client.ts \
       apps/monitor-dashboard/src/routes/dynamic-config.ts \
       apps/monitor-dashboard/src/index.ts \
       apps/monitor-dashboard-web/src/pages/DynamicConfig.tsx \
       apps/monitor-dashboard-web/src/App.tsx
git commit -m "feat(dashboard): add dynamic config management page"
```

---

### Task 7: 部署验证 + 文档更新

**Files:**
- Modify: `CLAUDE.md` (追加 Dynamic Config 说明)

- [ ] **Step 1: paas-engine 全量测试**

Run: `cd apps/paas-engine && go test ./... -v`
Expected: 全部 PASS

- [ ] **Step 2: 部署 paas-engine 到泳道**

部署 paas-engine（新增了 DB 表和 API 端点）：
```bash
make deploy APP=paas-engine LANE=feat-dynamic-config-by-lane GIT_REF=feat/dynamic-config-by-lane
```

- [ ] **Step 3: 验证 API**

用 `/api-test` skill 测试：
1. `PUT /api/paas/dynamic-config/default_model` body `{"lane":"prod","value":"gemini"}` — 设置基线
2. `PUT /api/paas/dynamic-config/default_model` body `{"lane":"dev","value":"gpt-4o"}` — 设置 dev 覆盖
3. `GET /internal/dynamic-config/resolved?lane=prod` — 应返回 gemini
4. `GET /internal/dynamic-config/resolved?lane=dev` — 应返回 gpt-4o
5. `GET /internal/dynamic-config/resolved?lane=staging` — 应 fallback 到 gemini
6. `DELETE /api/paas/dynamic-config/default_model?lane=dev` — 删除 dev 覆盖
7. `GET /internal/dynamic-config/resolved?lane=dev` — 应 fallback 到 gemini

- [ ] **Step 4: 部署 dashboard 到泳道并验证页面**

```bash
make deploy APP=monitor-dashboard LANE=feat-dynamic-config-by-lane GIT_REF=feat/dynamic-config-by-lane
make deploy APP=monitor-dashboard-web LANE=feat-dynamic-config-by-lane GIT_REF=feat/dynamic-config-by-lane
```

在 Dashboard 页面验证动态配置管理页面。

- [ ] **Step 5: 更新 CLAUDE.md**

在 `CLAUDE.md` 的 `## 核心数据流` 段落之后追加：
```markdown
### 动态配置

```
Dashboard → monitor-dashboard → paas-engine (管理 API, /api/paas/dynamic-config/)
                                            ↕ dynamic_configs 表
SDK (agent-service/lark-server) → paas-engine (读取 API, /internal/dynamic-config/resolved)
```

- 基础设施连接（DB/Redis）走 ConfigBundle（部署时环境变量）
- 业务行为参数（模型/阈值/flag）走 Dynamic Config（运行时 SDK 读取，10s 缓存）
- SDK 用法：`dynamic_config.get("key", default="value")`，lane 从 context 自动获取
```

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add dynamic config system documentation"
```
