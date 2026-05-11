# Dev Workflow v2 Phase 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 coe-* lane 部署时业务 pod 自动连 chiwei-test 基建、自动有 schema、operator 漏配 fail-closed。

**Architecture:** paas-engine ConfigBundle 加 ClassOverrides（lane class → key → value）+ RequiredKeys（强制完整覆盖校验）；App 加 AllowedLaneClasses（lark-proxy 禁部署 coe-*/ppe-*）；agent-service 抽 `ensure_business_schema()` 在 HTTP lifespan + worker entry 都调用，仅 coe-* 触发；新建 lark-server-runtime ConfigBundle 隔离 SYNCHRONIZE_DB=true。

**Tech Stack:** Go 1.23 (paas-engine, GORM AutoMigrate, JSONB 持久化), Python 3.12 (agent-service, SQLAlchemy 2.x async, FastAPI lifespan), Bun TS (lark-server, TypeORM), PostgreSQL, RabbitMQ, Redis, K8s/k3s.

**Spec:** `docs/superpowers/specs/2026-05-11-dev-workflow-v2-phase-2-design.md`

---

## File Map

### paas-engine 修改

- `apps/paas-engine/internal/domain/config_bundle.go` — 加 `ClassOverrides` + `RequiredKeys` 字段
- `apps/paas-engine/internal/domain/app.go` — 加 `AllowedLaneClasses` 字段
- `apps/paas-engine/internal/repository/config_bundle_repository.go` — model + serializer 加新字段
- `apps/paas-engine/internal/repository/app_repository.go` — model + serializer 加 AllowedLaneClasses
- `apps/paas-engine/internal/service/config_bundle_service.go` — ResolveBundleEnvs / ResolveConfig 加 class override 维度 + 新增 ValidateRequiredKeys
- `apps/paas-engine/internal/service/release_service.go` — CreateOrUpdateRelease 串行 RequiredKeys + AllowedLaneClasses 校验
- `apps/paas-engine/internal/domain/lane.go` — 已有 `LaneClass.String()`，本 plan 复用
- 测试：每个改动配套 _test.go

### agent-service 修改/新增

- `apps/agent-service/app/data/bootstrap.py` — **新建**：`ensure_business_schema()`
- `apps/agent-service/app/main.py` — lifespan 起手调用
- `apps/agent-service/app/workers/runtime_entry.py` — main 起手调用
- 测试：`apps/agent-service/tests/data/test_bootstrap.py` 新建

### lark-server / lark-proxy

无代码改动，所有变更通过 paas-engine ConfigBundle 配置

### 配置（运行时数据，不入 git）

通过 PaaS API：
- 创建 `lark-server-runtime` ConfigBundle（baseline `SYNCHRONIZE_DB=false`，coe override `true`）
- 给 lark-server / recall-worker / chat-response-worker 三个 App 加 `lark-server-runtime` bundle 引用
- 给 lark-proxy App 设 `AllowedLaneClasses=["prod"]`
- 给 pg-main / redis / rabbitmq / lark-server-runtime 配 `ClassOverrides[coe]`
- 给 pg-main / redis / rabbitmq / lark-server-runtime 配 `RequiredKeys[coe]`

---

## Rollout 顺序约束

按 spec 的 Rollout 顺序段，task 分阶段：

- **A 阶段（Task 1-7）**：paas-engine 代码改动（domain + persist + resolve + 校验函数 + handler）—— 全部通过单测验证、不动 prod 行为（RequiredKeys 字段空 = 校验不触发）
- **B 阶段（Task 8-10）**：agent-service 代码改动（schema bootstrap）—— 单测验证、prod 行为不变（守门只 coe-* 触发）
- **C 阶段（Task 11）**：deploy paas-engine + agent-service 到 prod（带新代码、配置全空、行为零变化）
- **D 阶段（Task 12-14）**：通过 PaaS API 配置 ClassOverrides → 配置 RequiredKeys → 配置 AllowedLaneClasses（按 spec rollout 顺序，先 override 再校验，避免硬拒）
- **E 阶段（Task 15）**：端到端验证（部署 coe-validation lane + 反向验证）

---

## Task 1: ConfigBundle domain 加 ClassOverrides + RequiredKeys

**Files:**
- Modify: `apps/paas-engine/internal/domain/config_bundle.go`
- Test: `apps/paas-engine/internal/domain/config_bundle_test.go` (新建或扩展)

- [ ] **Step 1: Write failing test for ClassOverrides + RequiredKeys 字段存在**

```go
// apps/paas-engine/internal/domain/config_bundle_test.go
package domain

import "testing"

func TestConfigBundle_ClassOverridesField(t *testing.T) {
	b := ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "chiwei-test-postgres"},
		},
	}
	got, ok := b.ClassOverrides["coe"]["POSTGRES_HOST"]
	if !ok || got != "chiwei-test-postgres" {
		t.Fatalf("ClassOverrides[coe][POSTGRES_HOST] = %q, want chiwei-test-postgres", got)
	}
}

func TestConfigBundle_RequiredKeysField(t *testing.T) {
	b := ConfigBundle{
		Name:         "pg-main",
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST", "POSTGRES_DB"}},
	}
	got := b.RequiredKeys["coe"]
	if len(got) != 2 {
		t.Fatalf("RequiredKeys[coe] len = %d, want 2", len(got))
	}
}
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd apps/paas-engine && go test ./internal/domain/ -run "TestConfigBundle_(Class|Required)" -v
```

Expected: FAIL with `undefined: ConfigBundle.ClassOverrides` / `undefined: ConfigBundle.RequiredKeys`

- [ ] **Step 3: Implement — 加字段到 struct**

Edit `apps/paas-engine/internal/domain/config_bundle.go`:

```go
package domain

import "time"

// ConfigBundle 表示一组按基础设施实例分组的配置项。
// 每个 key 是最终注入容器的环境变量名（如 PG_MAIN_HOST）。
type ConfigBundle struct {
	Name           string                       `json:"name"`
	Description    string                       `json:"description,omitempty"`
	Keys           map[string]string            `json:"keys,omitempty"`
	ClassOverrides map[string]map[string]string `json:"class_overrides,omitempty"` // lane class → key → value
	LaneOverrides  map[string]map[string]string `json:"lane_overrides,omitempty"`
	RequiredKeys   map[string][]string          `json:"required_keys,omitempty"` // lane class → 必须 override 的 key list
	ReferencedBy   []string                     `json:"referenced_by,omitempty"`
	CreatedAt      time.Time                    `json:"created_at"`
	UpdatedAt      time.Time                    `json:"updated_at"`
}
```

- [ ] **Step 4: Run test to verify pass**

```bash
cd apps/paas-engine && go test ./internal/domain/ -run "TestConfigBundle_(Class|Required)" -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/paas-engine/internal/domain/config_bundle.go apps/paas-engine/internal/domain/config_bundle_test.go
git commit -m "feat(paas-engine): ConfigBundle 加 ClassOverrides + RequiredKeys 字段

Phase 2 spec: docs/superpowers/specs/2026-05-11-dev-workflow-v2-phase-2-design.md

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: ConfigBundle repository 持久化新字段

GORM AutoMigrate 会自动加列。需在 model 上加对应 JSON 列 + serializer 改写 marshal/unmarshal。

**Files:**
- Modify: `apps/paas-engine/internal/repository/config_bundle_repository.go`
- Test: `apps/paas-engine/internal/repository/config_bundle_repository_test.go`

- [ ] **Step 1: Read repository 现状了解 model 结构**

```bash
grep -n "ConfigBundleModel\|bundleToModel\|modelToBundle\|LaneOverrides" apps/paas-engine/internal/repository/config_bundle_repository.go
```

记下 model struct 定义、bundleToModel / modelToBundle 函数位置和 JSON 编码模式。

- [ ] **Step 2: Write failing test — round-trip ClassOverrides + RequiredKeys**

加到 `apps/paas-engine/internal/repository/config_bundle_repository_test.go`（参考已有的 LaneOverrides round-trip 测试模式）：

```go
func TestConfigBundleRepository_RoundTripClassOverridesAndRequiredKeys(t *testing.T) {
	repo := newTestBundleRepo(t)  // 复用既有的 in-memory / sqlite test helper
	ctx := context.Background()

	bundle := &domain.ConfigBundle{
		Name:           "pg-main-test",
		Keys:           map[string]string{"POSTGRES_HOST": "postgres"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "chiwei-test-postgres", "POSTGRES_DB": "chiwei_test"},
		},
		RequiredKeys: map[string][]string{
			"coe": {"POSTGRES_HOST", "POSTGRES_DB"},
		},
	}
	if err := repo.Create(ctx, bundle); err != nil {
		t.Fatal(err)
	}
	got, err := repo.FindByName(ctx, "pg-main-test")
	if err != nil {
		t.Fatal(err)
	}
	if got.ClassOverrides["coe"]["POSTGRES_HOST"] != "chiwei-test-postgres" {
		t.Fatalf("ClassOverrides round-trip failed: %+v", got.ClassOverrides)
	}
	if got.ClassOverrides["coe"]["POSTGRES_DB"] != "chiwei_test" {
		t.Fatalf("ClassOverrides POSTGRES_DB round-trip failed: %+v", got.ClassOverrides)
	}
	if len(got.RequiredKeys["coe"]) != 2 {
		t.Fatalf("RequiredKeys round-trip failed: %+v", got.RequiredKeys)
	}
}
```

- [ ] **Step 3: Run test to verify fail**

```bash
cd apps/paas-engine && go test ./internal/repository/ -run TestConfigBundleRepository_RoundTripClassOverridesAndRequiredKeys -v
```

Expected: FAIL（model 没字段、serializer 不处理）

- [ ] **Step 4: 给 ConfigBundleModel 加 JSON 列**

按 Step 1 看到的现有 LaneOverrides 列模式（应该是 `gorm:"type:text"` 存 JSON string），加两个对称字段：

```go
type ConfigBundleModel struct {
	// ... 现有字段
	ClassOverrides string `gorm:"type:text"` // JSON: map[string]map[string]string
	RequiredKeys   string `gorm:"type:text"` // JSON: map[string][]string
}
```

- [ ] **Step 5: serializer 加 marshal/unmarshal**

bundleToModel：

```go
func bundleToModel(b *domain.ConfigBundle) (*ConfigBundleModel, error) {
	// ... 现有 keys / lane_overrides marshal
	classOverridesJSON, err := json.Marshal(b.ClassOverrides)
	if err != nil {
		return nil, fmt.Errorf("marshal ClassOverrides: %w", err)
	}
	requiredKeysJSON, err := json.Marshal(b.RequiredKeys)
	if err != nil {
		return nil, fmt.Errorf("marshal RequiredKeys: %w", err)
	}
	return &ConfigBundleModel{
		// ... 现有字段
		ClassOverrides: string(classOverridesJSON),
		RequiredKeys:   string(requiredKeysJSON),
	}, nil
}
```

modelToBundle：

```go
func modelToBundle(m *ConfigBundleModel) (*domain.ConfigBundle, error) {
	// ... 现有 keys / lane_overrides unmarshal
	classOverrides := map[string]map[string]string{}
	if m.ClassOverrides != "" {
		if err := json.Unmarshal([]byte(m.ClassOverrides), &classOverrides); err != nil {
			return nil, fmt.Errorf("unmarshal ClassOverrides: %w", err)
		}
	}
	requiredKeys := map[string][]string{}
	if m.RequiredKeys != "" {
		if err := json.Unmarshal([]byte(m.RequiredKeys), &requiredKeys); err != nil {
			return nil, fmt.Errorf("unmarshal RequiredKeys: %w", err)
		}
	}
	return &domain.ConfigBundle{
		// ... 现有字段
		ClassOverrides: classOverrides,
		RequiredKeys:   requiredKeys,
	}, nil
}
```

- [ ] **Step 6: Run test to verify pass**

```bash
cd apps/paas-engine && go test ./internal/repository/ -run TestConfigBundleRepository_RoundTripClassOverridesAndRequiredKeys -v
```

Expected: PASS

- [ ] **Step 7: 全 repo 测试 regression**

```bash
cd apps/paas-engine && go test ./internal/repository/ -v
```

Expected: 全部 PASS

- [ ] **Step 8: Commit**

```bash
git add apps/paas-engine/internal/repository/config_bundle_repository.go apps/paas-engine/internal/repository/config_bundle_repository_test.go
git commit -m "feat(paas-engine): ConfigBundle 持久化 ClassOverrides + RequiredKeys

GORM AutoMigrate 自动加 text 列；JSON marshal/unmarshal 双向。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: ResolveBundleEnvs / ResolveConfig 加 class override

新优先级（低 → 高）：`baseline → class override → lane override`（bundle 层）；完整 ResolveConfig：`bundle baseline → class override → lane override → app.Envs → release.Envs → auto-injected`。

**Files:**
- Modify: `apps/paas-engine/internal/service/config_bundle_service.go:283-361`
- Test: `apps/paas-engine/internal/service/config_bundle_service_test.go`

- [ ] **Step 1: Write failing tests — 5 层优先级**

加到 `config_bundle_service_test.go`：

```go
func TestResolveBundleEnvs_ClassOverridesAppliesAfterBaseline(t *testing.T) {
	bundles := []*domain.ConfigBundle{{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres", "POSTGRES_DB": "chiwei"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "chiwei-test-postgres"},
		},
	}}
	app := &domain.App{Name: "agent-service", ConfigBundles: []string{"pg-main"}}
	svc := newSvcWithBundles(t, bundles)  // 复用已有 newServiceForTest helper

	envs, err := svc.ResolveBundleEnvs(context.Background(), app, "coe-foo")
	if err != nil {
		t.Fatal(err)
	}
	if envs["POSTGRES_HOST"] != "chiwei-test-postgres" {
		t.Fatalf("class override not applied: HOST=%q", envs["POSTGRES_HOST"])
	}
	if envs["POSTGRES_DB"] != "chiwei" {
		t.Fatalf("baseline POSTGRES_DB lost: %q", envs["POSTGRES_DB"])
	}
}

func TestResolveBundleEnvs_LaneOverrideBeatsClassOverride(t *testing.T) {
	bundles := []*domain.ConfigBundle{{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "chiwei-test-postgres"},
		},
		LaneOverrides: map[string]map[string]string{
			"coe-foo": {"POSTGRES_HOST": "coe-foo-special"},
		},
	}}
	app := &domain.App{Name: "agent-service", ConfigBundles: []string{"pg-main"}}
	svc := newSvcWithBundles(t, bundles)

	envs, _ := svc.ResolveBundleEnvs(context.Background(), app, "coe-foo")
	if envs["POSTGRES_HOST"] != "coe-foo-special" {
		t.Fatalf("lane override should beat class: HOST=%q", envs["POSTGRES_HOST"])
	}
}

func TestResolveBundleEnvs_ProdLaneNoClassOverride(t *testing.T) {
	bundles := []*domain.ConfigBundle{{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "chiwei-test-postgres"},
		},
	}}
	app := &domain.App{Name: "agent-service", ConfigBundles: []string{"pg-main"}}
	svc := newSvcWithBundles(t, bundles)

	envs, _ := svc.ResolveBundleEnvs(context.Background(), app, "prod")
	if envs["POSTGRES_HOST"] != "postgres" {
		t.Fatalf("prod should get baseline: HOST=%q", envs["POSTGRES_HOST"])
	}
}
```

也加 ResolveConfig 对应的（验证 source 标签 `pg-main[class:coe]`）：

```go
func TestResolveConfig_ClassOverrideHasCorrectSource(t *testing.T) {
	// setup app + bundle 同上、设置 release for coe-foo
	// ...
	resolved, _ := svc.ResolveConfig(ctx, "agent-service", "coe-foo")
	entry := resolved["POSTGRES_HOST"]
	if entry.Source != "pg-main[class:coe]" {
		t.Fatalf("source = %q, want pg-main[class:coe]", entry.Source)
	}
}
```

- [ ] **Step 2: Run tests to verify fail**

```bash
cd apps/paas-engine && go test ./internal/service/ -run "TestResolveBundleEnvs_ClassOverrides|TestResolveBundleEnvs_LaneOverrideBeats|TestResolveBundleEnvs_ProdLaneNo|TestResolveConfig_ClassOverrideHasCorrect" -v
```

Expected: FAIL（class override 还没实现）

- [ ] **Step 3: Implement — ResolveBundleEnvs 加 class override**

Edit `apps/paas-engine/internal/service/config_bundle_service.go:339-361`：

```go
func (s *ConfigBundleService) ResolveBundleEnvs(ctx context.Context, app *domain.App, lane string) (map[string]string, error) {
	if len(app.ConfigBundles) == 0 {
		return nil, nil
	}

	bundles, err := s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
	if err != nil {
		return nil, err
	}

	// 用 ClassifyLane 确定 lane class（lane 已经在 ReleaseService 校验过）
	class, _ := domain.ClassifyLane(lane, s.cfg.LegacyLaneWhitelist)
	classKey := class.String()

	result := make(map[string]string)
	for _, bundle := range bundles {
		// baseline
		for k, v := range bundle.Keys {
			result[k] = v
		}
		// class override
		if classOverrides, ok := bundle.ClassOverrides[classKey]; ok {
			for k, v := range classOverrides {
				result[k] = v
			}
		}
		// lane override
		if laneOverrides, ok := bundle.LaneOverrides[lane]; ok {
			for k, v := range laneOverrides {
				result[k] = v
			}
		}
	}
	return result, nil
}
```

注意：`ConfigBundleService` 当前没有 cfg 字段（NewConfigBundleService 只接受 3 个 repo 参数）。本 task 加：

1. 新建 `ConfigBundleServiceConfig` type 在 `config_bundle_service.go`：

```go
type ConfigBundleServiceConfig struct {
	LegacyLaneWhitelist []string
}
```

2. ConfigBundleService struct 加 `cfg ConfigBundleServiceConfig` 字段
3. `NewConfigBundleService` 签名加第 4 参数 `cfg ConfigBundleServiceConfig`
4. 改 `apps/paas-engine/cmd/paas-engine/main.go:79` 的 `NewConfigBundleService` 调用，传 `service.ConfigBundleServiceConfig{LegacyLaneWhitelist: cfg.LegacyLaneWhitelist}`

- [ ] **Step 4: Implement — ResolveConfig 加 class override（同模式）**

Edit `apps/paas-engine/internal/service/config_bundle_service.go:283-335`：

在 `// 1. Bundle baseline + lane override` 段，把 baseline + lane override 之间插入 class override 块：

```go
// 1. Bundle baseline + class override + lane override
if len(app.ConfigBundles) > 0 {
	bundles, err := s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
	if err != nil {
		return nil, err
	}
	class, _ := domain.ClassifyLane(lane, s.cfg.LegacyLaneWhitelist)
	classKey := class.String()
	for _, bundle := range bundles {
		// baseline
		for k, v := range bundle.Keys {
			result[k] = ResolvedConfigEntry{Value: v, Source: bundle.Name}
		}
		// class override
		if classOverrides, ok := bundle.ClassOverrides[classKey]; ok {
			for k, v := range classOverrides {
				result[k] = ResolvedConfigEntry{Value: v, Source: bundle.Name + "[class:" + classKey + "]"}
			}
		}
		// lane override
		if overrides, ok := bundle.LaneOverrides[lane]; ok {
			for k, v := range overrides {
				result[k] = ResolvedConfigEntry{Value: v, Source: bundle.Name + "[lane:" + lane + "]"}
			}
		}
	}
}
```

- [ ] **Step 5: Run tests to verify pass**

```bash
cd apps/paas-engine && go test ./internal/service/ -v
```

Expected: 全部 PASS（含已有所有 service 测试 + 新加的 4 个 class override 测试）

- [ ] **Step 6: Commit**

```bash
git add apps/paas-engine/internal/service/config_bundle_service.go apps/paas-engine/internal/service/config_bundle_service_test.go apps/paas-engine/cmd/paas-engine/main.go
git commit -m "feat(paas-engine): ResolveBundleEnvs/ResolveConfig 加 class override 维度

新优先级 baseline → class override → lane override → app → release。
class 用 domain.ClassifyLane 解析；prod/blue → baseline，coe-* → ClassOverrides[coe]。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: ConfigBundleService.ValidateRequiredKeys 函数

独立的纯函数，被 ReleaseService 在 deploy 前调用。先单独写 + 测试，下个 task 集成。

**Files:**
- Modify: `apps/paas-engine/internal/service/config_bundle_service.go`
- Test: `apps/paas-engine/internal/service/config_bundle_service_test.go`

- [ ] **Step 1: Write failing tests — RequiredKeys 各场景**

```go
func TestValidateRequiredKeys_AllKeysOverridden_Pass(t *testing.T) {
	bundles := []*domain.ConfigBundle{{
		Name: "pg-main",
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "test-pg", "POSTGRES_DB": "chiwei_test"},
		},
		RequiredKeys: map[string][]string{
			"coe": {"POSTGRES_HOST", "POSTGRES_DB"},
		},
	}}
	if err := ValidateRequiredKeys(bundles, "coe"); err != nil {
		t.Fatalf("expected pass, got %v", err)
	}
}

func TestValidateRequiredKeys_KeyMissing_Reject(t *testing.T) {
	bundles := []*domain.ConfigBundle{{
		Name: "pg-main",
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "test-pg"}, // POSTGRES_DB 漏了
		},
		RequiredKeys: map[string][]string{
			"coe": {"POSTGRES_HOST", "POSTGRES_DB"},
		},
	}}
	err := ValidateRequiredKeys(bundles, "coe")
	if err == nil {
		t.Fatal("expected reject, got nil")
	}
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("error must wrap ErrInvalidInput for HTTP 400 mapping: %v", err)
	}
	// error message 必须明示 bundle name + key name
	if !strings.Contains(err.Error(), "pg-main") || !strings.Contains(err.Error(), "POSTGRES_DB") {
		t.Fatalf("error must mention bundle and key: %v", err)
	}
}

func TestValidateRequiredKeys_KeyEmptyValue_Reject(t *testing.T) {
	bundles := []*domain.ConfigBundle{{
		Name: "pg-main",
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "test-pg", "POSTGRES_DB": ""}, // 空值视同缺
		},
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST", "POSTGRES_DB"}},
	}}
	if err := ValidateRequiredKeys(bundles, "coe"); err == nil {
		t.Fatal("empty value should reject")
	}
}

func TestValidateRequiredKeys_ProdClass_NoCheck(t *testing.T) {
	// prod 没有 RequiredKeys 配置 → 不校验
	bundles := []*domain.ConfigBundle{{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres"},
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST"}},
	}}
	if err := ValidateRequiredKeys(bundles, "prod"); err != nil {
		t.Fatalf("prod class should not trigger coe RequiredKeys: %v", err)
	}
}

func TestValidateRequiredKeys_NoRequiredKeys_NoCheck(t *testing.T) {
	bundles := []*domain.ConfigBundle{{
		Name: "inter-service-auth",
		Keys: map[string]string{"AUTH_TOKEN": "xxx"},
		// 没有 RequiredKeys 字段 → 不校验
	}}
	if err := ValidateRequiredKeys(bundles, "coe"); err != nil {
		t.Fatalf("bundle without RequiredKeys should pass: %v", err)
	}
}
```

- [ ] **Step 2: Run tests to verify fail**

```bash
cd apps/paas-engine && go test ./internal/service/ -run TestValidateRequiredKeys -v
```

Expected: FAIL（函数不存在）

- [ ] **Step 3: Implement ValidateRequiredKeys 函数**

加到 `apps/paas-engine/internal/service/config_bundle_service.go` 文件末尾：

```go
// ValidateRequiredKeys 校验 bundles 中标记 RequiredKeys[class] 的 key 都在 ClassOverrides[class] 里有非空值。
// 任一 key 缺失或空值 → 返回 wrap ErrInvalidInput 的 error，明示 bundle + key。
// 若所有 bundle 都没声明 RequiredKeys[class]，直接 pass（无校验对象）。
func ValidateRequiredKeys(bundles []*domain.ConfigBundle, classKey string) error {
	for _, bundle := range bundles {
		required, ok := bundle.RequiredKeys[classKey]
		if !ok || len(required) == 0 {
			continue
		}
		overrides := bundle.ClassOverrides[classKey]
		for _, key := range required {
			val, present := overrides[key]
			if !present || val == "" {
				return fmt.Errorf(
					"%w: bundle %q requires class %q to override key %q (currently missing or empty); operator must set ClassOverrides[%s][%s] before deploying %s lanes",
					domain.ErrInvalidInput, bundle.Name, classKey, key, classKey, key, classKey,
				)
			}
		}
	}
	return nil
}
```

- [ ] **Step 4: Run tests to verify pass**

```bash
cd apps/paas-engine && go test ./internal/service/ -run TestValidateRequiredKeys -v
```

Expected: PASS（5 个 case）

- [ ] **Step 5: Commit**

```bash
git add apps/paas-engine/internal/service/config_bundle_service.go apps/paas-engine/internal/service/config_bundle_service_test.go
git commit -m "feat(paas-engine): ValidateRequiredKeys 校验函数

强制 ClassOverrides[class] 必须完整覆盖 bundle.RequiredKeys[class] 列出的 key；空值视同缺。
wrap ErrInvalidInput 让 HTTP handler 自动 400。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: ReleaseService.CreateOrUpdateRelease 集成 RequiredKeys 校验

**Files:**
- Modify: `apps/paas-engine/internal/service/release_service.go:63-150`（在 ResolveBundleEnvs 之前插入校验）
- Test: `apps/paas-engine/internal/service/release_service_test.go`

- [ ] **Step 1: Write failing test**

```go
func TestCreateOrUpdateRelease_RejectsCoeWithMissingRequiredKey(t *testing.T) {
	bundle := &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres", "POSTGRES_DB": "chiwei"},
		ClassOverrides: map[string]map[string]string{
			"coe": {"POSTGRES_HOST": "test-pg"}, // POSTGRES_DB 漏了
		},
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST", "POSTGRES_DB"}},
	}
	app := &domain.App{Name: "agent-service", ConfigBundles: []string{"pg-main"}}
	svc := newReleaseSvc(t, []*domain.App{app}, []*domain.ConfigBundle{bundle})

	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "agent-service",
		Lane:     "coe-foo",
		ImageTag: "1.0.0",
	})
	if err == nil {
		t.Fatal("expected reject for missing RequiredKey")
	}
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("error must wrap ErrInvalidInput: %v", err)
	}
}

func TestCreateOrUpdateRelease_AllowsProdEvenWithCoeRequiredKeys(t *testing.T) {
	// prod lane 不触发 coe RequiredKeys 校验
	bundle := &domain.ConfigBundle{
		Name: "pg-main",
		Keys: map[string]string{"POSTGRES_HOST": "postgres"},
		RequiredKeys: map[string][]string{"coe": {"POSTGRES_HOST", "POSTGRES_DB"}},
	}
	app := &domain.App{Name: "agent-service", ConfigBundles: []string{"pg-main"}}
	svc := newReleaseSvc(t, []*domain.App{app}, []*domain.ConfigBundle{bundle})

	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "agent-service",
		Lane:     "prod",
		ImageTag: "1.0.0",
	})
	if err != nil && errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("prod lane should not trigger coe RequiredKeys: %v", err)
	}
	// 注意：可能还有其他原因报错（比如 build 不存在），但只要不是 ErrInvalidInput from RequiredKeys 即可
}
```

- [ ] **Step 2: Run tests to verify fail**

```bash
cd apps/paas-engine && go test ./internal/service/ -run "TestCreateOrUpdateRelease_RejectsCoe|TestCreateOrUpdateRelease_AllowsProd" -v
```

Expected: FAIL（校验未集成）

- [ ] **Step 3: Implement — 在 release_service.go ResolveBundleEnvs 调用前插入校验**

在 `apps/paas-engine/internal/service/release_service.go` 大约 line 145-148 现在的 `if s.configBundleSvc != nil && len(app.ConfigBundles) > 0` 块**之前**：

```go
// RequiredKeys 校验：根据 lane class 强制完整 override
// spec: docs/superpowers/specs/2026-05-11-dev-workflow-v2-phase-2-design.md §Fail-closed 部署校验
if s.configBundleSvc != nil && len(app.ConfigBundles) > 0 {
	class, classErr := domain.ClassifyLane(lane, s.cfg.LegacyLaneWhitelist)
	if classErr == nil {
		bundles, bErr := s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
		if bErr != nil {
			return nil, bErr
		}
		if vErr := ValidateRequiredKeys(bundles, class.String()); vErr != nil {
			return nil, fmt.Errorf("release create rejected: %w", vErr)
		}
	}
}
```

注意：`ReleaseService` 当前没有直接拿 bundle list 的渠道（它通过 `s.configBundleSvc.ResolveBundleEnvs` 拿合并后 envs，但拿不到原始 bundle 对象）。本 task 在 `ConfigBundleService` 加一个简单方法暴露给 ReleaseService：

加到 `apps/paas-engine/internal/service/config_bundle_service.go`：

```go
// GetBundlesForApp 拿 app 引用的所有 bundle（含 RequiredKeys/ClassOverrides 字段）。
// 用于 ReleaseService 在 deploy 前跑 ValidateRequiredKeys。
func (s *ConfigBundleService) GetBundlesForApp(ctx context.Context, app *domain.App) ([]*domain.ConfigBundle, error) {
	if len(app.ConfigBundles) == 0 {
		return nil, nil
	}
	return s.bundleRepo.FindByNames(ctx, app.ConfigBundles)
}
```

ReleaseService 调用：

```go
bundles, bErr := s.configBundleSvc.GetBundlesForApp(ctx, app)
```

- [ ] **Step 4: Run tests to verify pass**

```bash
cd apps/paas-engine && go test ./internal/service/ -v
```

Expected: PASS

- [ ] **Step 5: 全 paas-engine regression**

```bash
cd apps/paas-engine && go test ./... -v
```

Expected: 全 PASS（含 Phase 1 lane 校验等所有已有测试）

- [ ] **Step 6: Commit**

```bash
git add apps/paas-engine/internal/service/release_service.go apps/paas-engine/internal/service/release_service_test.go apps/paas-engine/internal/service/config_bundle_service.go
git commit -m "feat(paas-engine): CreateOrUpdateRelease 加 RequiredKeys 校验

部署前对 app 引用的所有 bundle 跑 ValidateRequiredKeys，缺 key 直接 reject。
prod/blue 走 LaneClassProd，bundle.RequiredKeys[\"prod\"] 通常为空 → 不触发。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: App.AllowedLaneClasses domain + repository

**Files:**
- Modify: `apps/paas-engine/internal/domain/app.go`
- Modify: `apps/paas-engine/internal/repository/app_repository.go`
- Test: `apps/paas-engine/internal/repository/app_repository_test.go`

- [ ] **Step 1: Write failing test**

```go
// apps/paas-engine/internal/repository/app_repository_test.go
func TestAppRepository_RoundTripAllowedLaneClasses(t *testing.T) {
	repo := newTestAppRepo(t)
	ctx := context.Background()

	app := &domain.App{
		Name:               "lark-proxy",
		ImageRepoName:      "lark-proxy",
		Port:               3003,
		AllowedLaneClasses: []string{"prod"},
	}
	if err := repo.Create(ctx, app); err != nil {
		t.Fatal(err)
	}
	got, err := repo.FindByName(ctx, "lark-proxy")
	if err != nil {
		t.Fatal(err)
	}
	if len(got.AllowedLaneClasses) != 1 || got.AllowedLaneClasses[0] != "prod" {
		t.Fatalf("AllowedLaneClasses round-trip failed: %+v", got.AllowedLaneClasses)
	}
}
```

- [ ] **Step 2: Run test to verify fail**

```bash
cd apps/paas-engine && go test ./internal/repository/ -run TestAppRepository_RoundTripAllowedLaneClasses -v
```

Expected: FAIL（field undefined）

- [ ] **Step 3: 加 AllowedLaneClasses 字段到 App domain**

Edit `apps/paas-engine/internal/domain/app.go`，在 ConfigBundles 字段后插入：

```go
type App struct {
	// ... 现有字段
	ConfigBundles      []string          `json:"config_bundles,omitempty"`
	AllowedLaneClasses []string          `json:"allowed_lane_classes,omitempty"` // 限制可部署的 lane class，nil 或空 = 全允许
	// ... 剩余字段
}
```

- [ ] **Step 4: 加 AppModel + serializer**

按 `app_repository.go` 现有的 ConfigBundles 列模式（应该是 JSON text 列），加 `AllowedLaneClasses`：

```go
type AppModel struct {
	// ... 现有字段
	AllowedLaneClasses string `gorm:"type:text"` // JSON: []string
}

// appToModel
allowedJSON, _ := json.Marshal(app.AllowedLaneClasses)
m.AllowedLaneClasses = string(allowedJSON)

// modelToApp
var allowed []string
if m.AllowedLaneClasses != "" {
	if err := json.Unmarshal([]byte(m.AllowedLaneClasses), &allowed); err != nil {
		return nil, fmt.Errorf("unmarshal AllowedLaneClasses: %w", err)
	}
}
app.AllowedLaneClasses = allowed
```

- [ ] **Step 5: Run tests to verify pass**

```bash
cd apps/paas-engine && go test ./internal/repository/ -v
```

Expected: 全 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/paas-engine/internal/domain/app.go apps/paas-engine/internal/repository/app_repository.go apps/paas-engine/internal/repository/app_repository_test.go
git commit -m "feat(paas-engine): App 加 AllowedLaneClasses 字段

为 lark-proxy 限制只能部署到 prod 类 lane 做准备。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: ReleaseService.CreateOrUpdateRelease 集成 AllowedLaneClasses 校验

**Files:**
- Modify: `apps/paas-engine/internal/service/release_service.go`
- Test: `apps/paas-engine/internal/service/release_service_test.go`

- [ ] **Step 1: Write failing tests**

```go
func TestCreateOrUpdateRelease_RejectsLarkProxyToCoe(t *testing.T) {
	app := &domain.App{
		Name:               "lark-proxy",
		ImageRepoName:      "lark-proxy",
		Port:               3003,
		AllowedLaneClasses: []string{"prod"},
	}
	svc := newReleaseSvc(t, []*domain.App{app}, nil)
	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "lark-proxy",
		Lane:     "coe-foo",
		ImageTag: "1.0.0",
	})
	if err == nil {
		t.Fatal("expected reject for lark-proxy to coe lane")
	}
	if !errors.Is(err, domain.ErrInvalidInput) {
		t.Fatalf("must wrap ErrInvalidInput: %v", err)
	}
	if !strings.Contains(err.Error(), "lark-proxy") || !strings.Contains(err.Error(), "coe") {
		t.Fatalf("error must mention app and lane class: %v", err)
	}
}

func TestCreateOrUpdateRelease_AllowsLarkProxyToProd(t *testing.T) {
	app := &domain.App{
		Name:               "lark-proxy",
		ImageRepoName:      "lark-proxy",
		Port:               3003,
		AllowedLaneClasses: []string{"prod"},
	}
	svc := newReleaseSvc(t, []*domain.App{app}, nil)
	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "lark-proxy",
		Lane:     "prod",
		ImageTag: "1.0.0",
	})
	// 注意：可能还有其他原因报错（build 不存在等），但不应该 wrap ErrInvalidInput from AllowedLaneClasses
	if err != nil && strings.Contains(err.Error(), "AllowedLaneClasses") {
		t.Fatalf("prod should not be rejected by AllowedLaneClasses: %v", err)
	}
}

func TestCreateOrUpdateRelease_AppWithoutAllowedLaneClasses_AllowsAll(t *testing.T) {
	// 没设 AllowedLaneClasses（nil）= 全允许（向后兼容现有 App）
	app := &domain.App{
		Name:          "agent-service",
		ImageRepoName: "agent-service",
		Port:          8000,
		// AllowedLaneClasses 不设
	}
	svc := newReleaseSvc(t, []*domain.App{app}, nil)
	_, err := svc.CreateOrUpdateRelease(context.Background(), CreateReleaseRequest{
		AppName:  "agent-service",
		Lane:     "coe-foo",
		ImageTag: "1.0.0",
	})
	if err != nil && strings.Contains(err.Error(), "AllowedLaneClasses") {
		t.Fatalf("nil AllowedLaneClasses should allow all: %v", err)
	}
}
```

- [ ] **Step 2: Run tests to verify fail**

```bash
cd apps/paas-engine && go test ./internal/service/ -run TestCreateOrUpdateRelease_(RejectsLarkProxy|AllowsLarkProxy|AppWithoutAllowed) -v
```

Expected: FAIL

- [ ] **Step 3: Implement — release_service.go 加校验**

在 `apps/paas-engine/internal/service/release_service.go` ClassifyLane 校验之后、ResolveBundleEnvs 之前插入：

```go
// AllowedLaneClasses 校验：限制 App 只能部署到指定 lane class
// spec: §lark-proxy 部署门禁
if len(app.AllowedLaneClasses) > 0 {
	class, classErr := domain.ClassifyLane(lane, s.cfg.LegacyLaneWhitelist)
	if classErr == nil {
		classKey := class.String()
		allowed := false
		for _, c := range app.AllowedLaneClasses {
			if c == classKey {
				allowed = true
				break
			}
		}
		if !allowed {
			return nil, fmt.Errorf(
				"%w: app %q only allowed in lane classes %v, lane %q is class %q (AllowedLaneClasses)",
				domain.ErrInvalidInput, app.Name, app.AllowedLaneClasses, lane, classKey,
			)
		}
	}
}
```

- [ ] **Step 4: Run tests to verify pass**

```bash
cd apps/paas-engine && go test ./internal/service/ -v
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/paas-engine/internal/service/release_service.go apps/paas-engine/internal/service/release_service_test.go
git commit -m "feat(paas-engine): CreateOrUpdateRelease 加 AllowedLaneClasses 校验

限制 lark-proxy 只能部署到 prod 类 lane（AllowedLaneClasses=[\"prod\"]）。
其他 App 不设此字段（nil）= 全允许，向后兼容。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: agent-service ensure_business_schema 函数

**Files:**
- Create: `apps/agent-service/app/data/bootstrap.py`
- Test: `apps/agent-service/tests/data/test_bootstrap.py`

- [ ] **Step 1: 确定 SQLAlchemy Base 在哪个 module + engine 怎么拿**

```bash
grep -rn "DeclarativeBase\|declarative_base\|class Base" apps/agent-service/app/data/ 2>/dev/null | head -10
grep -rn "create_async_engine\|engine =" apps/agent-service/app/data/session.py 2>/dev/null | head -10
```

记下 Base 的 import path（应是 `app.data.models.Base` 或 `app.data.base.Base`）和 engine 拿法（应是 `app.data.session.engine` 或 `app.data.session.get_engine()`）。

- [ ] **Step 2: Write failing test**

新建 `apps/agent-service/tests/data/test_bootstrap.py`：

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from app.data.bootstrap import ensure_business_schema


@pytest.mark.asyncio
async def test_ensure_business_schema_triggers_for_coe_lane():
    mock_engine = AsyncMock()
    mock_conn = AsyncMock()
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn

    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "coe-foo"
        await ensure_business_schema()

    mock_conn.run_sync.assert_called_once()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_prod():
    mock_engine = AsyncMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "prod"
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_blue():
    mock_engine = AsyncMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "blue"
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_ppe():
    """ppe-* 连 prod 基建，绝不能跑 create_all（会在 prod DB 上跑）"""
    mock_engine = AsyncMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "ppe-canary"
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_skips_for_none_lane():
    """LANE env 没注入（None）也不跑 create_all"""
    mock_engine = AsyncMock()
    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = None
        await ensure_business_schema()
    mock_engine.begin.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_business_schema_failure_raises():
    """create_all 失败必须 raise 让 pod CrashLoopBackoff，绝不 swallow"""
    mock_engine = AsyncMock()
    mock_conn = AsyncMock()
    mock_engine.begin.return_value.__aenter__.return_value = mock_conn
    mock_conn.run_sync.side_effect = Exception("PG connection refused")

    with patch("app.data.bootstrap.engine", mock_engine), \
         patch("app.data.bootstrap.settings") as mock_settings:
        mock_settings.lane = "coe-foo"
        with pytest.raises(Exception, match="PG connection refused"):
            await ensure_business_schema()
```

- [ ] **Step 3: Run tests to verify fail**

```bash
cd apps/agent-service && uv run pytest tests/data/test_bootstrap.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.data.bootstrap'`

- [ ] **Step 4: Implement bootstrap.py**

新建 `apps/agent-service/app/data/bootstrap.py`：

```python
"""coe-* lane 业务表自动建。

spec: docs/superpowers/specs/2026-05-11-dev-workflow-v2-phase-2-design.md §agent-service coe-* lane 自动建表
"""
import logging

from app.data.models import Base
from app.data.session import engine
from app.infra.config import settings

logger = logging.getLogger(__name__)


async def ensure_business_schema() -> None:
    """仅 coe-* lane 触发 SQLAlchemy Base.metadata.create_all。

    严格白名单守门：prod / blue / ppe-* / None lane 一律不建表。
    create_all 失败必须 raise（不 swallow）让 pod CrashLoopBackoff。
    """
    lane = settings.lane
    if not lane or not lane.startswith("coe-"):
        return
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        logger.exception("auto create_all failed for coe lane %s, aborting startup", lane)
        raise
```

注意：Base / engine / settings 三个 import 路径必须对齐 Step 1 grep 出来的实际路径。如果 Base 不在 `app.data.models`（比如在 `app.data.base`），调整 import。

- [ ] **Step 5: Run tests to verify pass**

```bash
cd apps/agent-service && uv run pytest tests/data/test_bootstrap.py -v
```

Expected: 6 个 case 全 PASS

- [ ] **Step 6: Commit**

```bash
git add apps/agent-service/app/data/bootstrap.py apps/agent-service/tests/data/test_bootstrap.py
git commit -m "feat(agent-service): ensure_business_schema 仅 coe-* lane 自动建表

白名单守门 startswith('coe-')；prod/blue/ppe-*/None 全不建。
失败 raise 让 pod CrashLoopBackoff，不 swallow。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: agent-service main.py lifespan 接入 ensure_business_schema

**Files:**
- Modify: `apps/agent-service/app/main.py`

- [ ] **Step 1: 读 main.py lifespan 现状**

```bash
grep -n "lifespan\|@asynccontextmanager\|async def lifespan" apps/agent-service/app/main.py
```

记下 lifespan 函数定义起止行号，以及现在 startup 期跑了什么。

- [ ] **Step 2: Write failing test**

加到 `apps/agent-service/tests/test_main_lifespan.py`（新建或扩展）：

```python
import pytest
from unittest.mock import AsyncMock, patch
from fastapi import FastAPI

@pytest.mark.asyncio
async def test_lifespan_calls_ensure_business_schema():
    with patch("app.main.ensure_business_schema", new=AsyncMock()) as mock_ensure:
        from app.main import lifespan
        app = FastAPI()
        async with lifespan(app):
            pass
        mock_ensure.assert_awaited_once()
```

- [ ] **Step 3: Run test to verify fail**

```bash
cd apps/agent-service && uv run pytest tests/test_main_lifespan.py -v
```

Expected: FAIL（没 import / 没调用）

- [ ] **Step 4: Implement — main.py 加 import + lifespan 调用**

Edit `apps/agent-service/app/main.py`：

import 段加：

```python
from app.data.bootstrap import ensure_business_schema
```

lifespan 函数体（startup 阶段）**最前面**加：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    # coe-* lane 业务表自动建（spec phase-2 §agent-service coe-* lane 自动建表）
    await ensure_business_schema()
    # ... 后续现有 startup 逻辑（migrate_schema、declare topology 等）
    yield
    # ... 现有 shutdown 逻辑
```

`ensure_business_schema` 必须在其他 startup 逻辑之前——后续 migrator / RabbitMQ topology declare / 业务 ready 都依赖表存在。

- [ ] **Step 5: Run test to verify pass**

```bash
cd apps/agent-service && uv run pytest tests/test_main_lifespan.py -v
```

Expected: PASS

- [ ] **Step 6: 全 agent-service test regression**

```bash
cd apps/agent-service && uv run pytest -x -q
```

Expected: 全 PASS（已有测试不受影响）

- [ ] **Step 7: Commit**

```bash
git add apps/agent-service/app/main.py apps/agent-service/tests/test_main_lifespan.py
git commit -m "feat(agent-service): main.py lifespan 接入 ensure_business_schema

HTTP 服务（agent-service Deployment）启动期跑 schema bootstrap。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: agent-service runtime_entry.py 接入 ensure_business_schema

vectorize-worker / 其他通过 runtime_entry.py 启动的 worker Deployment。

**Files:**
- Modify: `apps/agent-service/app/workers/runtime_entry.py`

- [ ] **Step 1: 读 runtime_entry.py 现状**

```bash
cat apps/agent-service/app/workers/runtime_entry.py
```

记下 main() 函数定义、Runtime.run() 在哪一行调用、是不是 sync/async。

- [ ] **Step 2: Write failing test**

新建 `apps/agent-service/tests/workers/test_runtime_entry.py`：

```python
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


@pytest.mark.asyncio
async def test_runtime_entry_main_calls_ensure_business_schema_first():
    """runtime_entry main 必须在 Runtime.run() 之前调用 ensure_business_schema"""
    call_order = []

    async def track_ensure():
        call_order.append("ensure")

    async def track_run(self):
        call_order.append("run")

    with patch("app.workers.runtime_entry.ensure_business_schema", side_effect=track_ensure), \
         patch("app.workers.runtime_entry.Runtime") as MockRuntime, \
         patch.dict("os.environ", {"APP_NAME": "vectorize-worker"}):
        MockRuntime.return_value.run = track_run.__get__(MockRuntime.return_value)
        from app.workers.runtime_entry import main
        await main()

    assert call_order == ["ensure", "run"], f"call order: {call_order}"
```

- [ ] **Step 3: Run test to verify fail**

```bash
cd apps/agent-service && uv run pytest tests/workers/test_runtime_entry.py -v
```

Expected: FAIL

- [ ] **Step 4: Implement — runtime_entry.py 加 import + 调用**

Edit `apps/agent-service/app/workers/runtime_entry.py`：

import 段：

```python
from app.data.bootstrap import ensure_business_schema
```

main() 起手（在 Runtime 构造之前）：

```python
async def main() -> None:
    # coe-* lane 业务表自动建（spec phase-2 §多 Deployment 同镜像 schema 启动顺序）
    await ensure_business_schema()
    app_name = os.environ["APP_NAME"]
    runtime = Runtime(app_name=app_name)
    await runtime.run()
```

如果 main() 是 sync 调用 asyncio.run(...)，调整签名让 ensure_business_schema 也跑在同一个 event loop 里。

- [ ] **Step 5: Run test to verify pass**

```bash
cd apps/agent-service && uv run pytest tests/workers/test_runtime_entry.py -v
```

Expected: PASS

- [ ] **Step 6: 全 agent-service test regression**

```bash
cd apps/agent-service && uv run pytest -x -q
```

Expected: 全 PASS

- [ ] **Step 7: Commit**

```bash
git add apps/agent-service/app/workers/runtime_entry.py apps/agent-service/tests/workers/test_runtime_entry.py
git commit -m "feat(agent-service): runtime_entry.py main 接入 ensure_business_schema

vectorize-worker 等 worker Deployment 启动期也跑 schema bootstrap。
位置在 Runtime.run() 之前，保证后续 dataflow 节点拿到的表已建。

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: deploy paas-engine + agent-service 到 prod（带新代码、配置全空）

按 spec rollout 顺序步骤 1：paas-engine 升级到带 ClassOverrides + RequiredKeys 字段的版本，但 RequiredKeys 字段在所有 bundle 上都还没配 = 校验空跑、prod 行为零变化。

- [ ] **Step 1: 确认当前分支 commit 推到 origin**

```bash
git push origin chore/enhance-dev-workflow
```

Expected: push 成功

- [ ] **Step 2: paas-engine 自部署（蓝绿）**

```bash
cd /data00/home/yuanzhihong.chiwei/code/personal/chiwei-platform-worktrees/chore-enhance-dev-workflow
make self-deploy BUMP=patch
```

Expected: paas-engine 在 prod 和 blue 两个 lane 都升级到含 ClassOverrides + RequiredKeys 字段的新版本。监控 logs 确认 GORM AutoMigrate 自动加了 `class_overrides` `required_keys` 列。

```bash
make logs APP=paas-engine SINCE=10m KEYWORD="AutoMigrate\|ALTER TABLE\|class_overrides\|required_keys" 2>&1 | head -30
```

- [ ] **Step 3: 验证 paas-engine 行为零变化（既有 bundle GET 应包含新字段，但都为空）**

```bash
curl -sS -H "X-API-Key: $PAAS_TOKEN" "$PAAS_API/api/paas/config-bundles/pg-main" | jq '{name, class_overrides, required_keys}'
```

Expected: `class_overrides: {}` 或 `null`，`required_keys: {}` 或 `null`

- [ ] **Step 4: 验证 ResolveConfig 行为零变化（prod lane 还拿 baseline）**

```bash
curl -sS -H "X-API-Key: $PAAS_TOKEN" "$PAAS_API/api/paas/apps/agent-service/resolved-config?lane=prod" | jq '.[] | select(.key=="POSTGRES_HOST")'
```

Expected: `value: "postgres"`, `source: "pg-main"`

- [ ] **Step 5: 部署 agent-service 到 prod**

```bash
make deploy APP=agent-service GIT_REF=chore/enhance-dev-workflow BUMP=patch
make deploy APP=vectorize-worker LANE=prod  # release 同一镜像到 vectorize-worker
```

Expected: agent-service + vectorize-worker 都拉新镜像。lifespan + runtime_entry 跑 ensure_business_schema 时 lane=prod → 直接 return，不建表。

- [ ] **Step 6: 验证 agent-service prod 启动 log 不含 create_all 调用**

```bash
make logs APP=agent-service SINCE=5m KEYWORD="ensure_business_schema\|create_all\|CREATE TABLE" 2>&1 | head -30
make logs APP=vectorize-worker SINCE=5m KEYWORD="ensure_business_schema\|create_all\|CREATE TABLE" 2>&1 | head -30
```

Expected: 不出现 CREATE TABLE 输出（prod lane 守门生效）

- [ ] **Step 7: 烟雾测试 prod 业务无回归**

发一条飞书消息给赤尾 prod bot，确认能正常回复。

Expected: 业务流程正常。

---

## Task 12: 通过 PaaS API 创建 lark-server-runtime bundle + 给 3 个 App 加引用

按 spec rollout 顺序步骤 2 第一部分：先创建 lark-server-runtime bundle（baseline=false 防漏到 prod），然后给 lark-server / recall-worker / chat-response-worker 三个 App 加引用。

- [ ] **Step 1: 创建 lark-server-runtime bundle**

```bash
curl -sS -X POST -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/" \
  -d '{
    "name": "lark-server-runtime",
    "description": "lark-server 镜像所有 Deployment 的 runtime 行为开关。SYNCHRONIZE_DB 仅在 coe-* lane 开启。",
    "keys": {
      "SYNCHRONIZE_DB": "false"
    }
  }' | jq .
```

Expected: 返回 bundle JSON 含 `name: lark-server-runtime`、`keys.SYNCHRONIZE_DB: "false"`

- [ ] **Step 2: 给 lark-server App 加 lark-server-runtime 引用（PUT merge 语义）**

先 GET 现状拿到完整 config_bundles 列表：

```bash
curl -sS -H "X-API-Key: $PAAS_TOKEN" "$PAAS_API/api/paas/apps/lark-server" | jq '.config_bundles'
```

Expected output: `["pg-main","redis","mongo","oss","inter-service-auth","rabbitmq","ai-provider"]`

PUT 加进去（注意：PUT 是 merge 语义，必须传完整 list）：

```bash
curl -sS -X PUT -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/apps/lark-server" \
  -d '{
    "config_bundles": ["pg-main","redis","mongo","oss","inter-service-auth","rabbitmq","ai-provider","lark-server-runtime"]
  }' | jq '.config_bundles'
```

Expected: 返回新列表含 `lark-server-runtime`

- [ ] **Step 3: 给 recall-worker App 加引用**

同 Step 2 模式：

```bash
curl -sS -X PUT -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/apps/recall-worker" \
  -d '{
    "config_bundles": ["pg-main","redis","mongo","oss","inter-service-auth","rabbitmq","ai-provider","lark-server-runtime"]
  }' | jq '.config_bundles'
```

- [ ] **Step 4: 给 chat-response-worker App 加引用**

```bash
curl -sS -X PUT -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/apps/chat-response-worker" \
  -d '{
    "config_bundles": ["pg-main","redis","mongo","oss","inter-service-auth","rabbitmq","ai-provider","lark-server-runtime"]
  }' | jq '.config_bundles'
```

- [ ] **Step 5: 验证 prod resolved-config 含 SYNCHRONIZE_DB=false**

```bash
for app in lark-server recall-worker chat-response-worker; do
  echo "=== $app ==="
  curl -sS -H "X-API-Key: $PAAS_TOKEN" "$PAAS_API/api/paas/apps/$app/resolved-config?lane=prod" \
    | jq '.[] | select(.key=="SYNCHRONIZE_DB")'
done
```

Expected: 三个 App 都返回 `value: "false"`, `source: "lark-server-runtime"`

- [ ] **Step 6: 重新部署三个 App 到 prod 让 SYNCHRONIZE_DB=false 生效**

```bash
for app in lark-server recall-worker chat-response-worker; do
  make deploy APP=$app LANE=prod GIT_REF=main
done
```

Expected: 三个 Deployment 重启，新 pod 拿到 `SYNCHRONIZE_DB=false` env，TypeORM 不 sync，prod schema 行为零变化。

- [ ] **Step 7: 烟雾测试 prod 业务无回归**

发一条飞书消息确认 chat-response-worker 能消费、lark-server 能发消息。

---

## Task 13: 配置 ClassOverrides + RequiredKeys（按 rollout 顺序）

按 spec rollout 顺序步骤 2 第二部分 + 步骤 3：先配 ClassOverrides，再配 RequiredKeys。

测试基建 host / port / 密码值在 cpu1 `~/.chiwei-test-env.env`，需 `ssh cpu1 cat ~/.chiwei-test-env.env` 拿。

- [ ] **Step 1: 拿测试基建连接信息**

```bash
ssh cpu1 cat ~/.chiwei-test-env.env
```

记下：
- `CHIWEI_TEST_PG_HOST` （cpu1 的内网 IP，应是 10.37.6.235）
- `CHIWEI_TEST_PG_PORT=5433`
- `CHIWEI_TEST_PG_USER` / `_PASSWORD` / `_DB`
- `CHIWEI_TEST_REDIS_HOST` (10.37.6.235)
- `CHIWEI_TEST_REDIS_PORT=6380`
- `CHIWEI_TEST_REDIS_PASSWORD`
- `CHIWEI_TEST_RABBITMQ_URL` (含端口 5673)

- [ ] **Step 2: 给 pg-main 配 ClassOverrides[coe]**

```bash
curl -sS -X PUT -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/pg-main/class-overrides/coe" \
  -d '{
    "POSTGRES_HOST": "10.37.6.235",
    "POSTGRES_PORT": "5433",
    "POSTGRES_USER": "<chiwei-test-pg-user>",
    "POSTGRES_PASSWORD": "<chiwei-test-pg-password>",
    "POSTGRES_DB": "<chiwei-test-pg-db>"
  }' | jq .
```

注：endpoint 路径如果还没实现（看 paas-engine handler 现有 lane-overrides API 模式），可能需要 PATCH 整个 bundle：

```bash
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/pg-main" \
  -d '{
    "class_overrides": {
      "coe": {
        "POSTGRES_HOST": "10.37.6.235",
        "POSTGRES_PORT": "5433",
        "POSTGRES_USER": "<...>",
        "POSTGRES_PASSWORD": "<...>",
        "POSTGRES_DB": "<...>"
      }
    }
  }' | jq .
```

如果 PATCH 也不支持，回退 PUT 整个 bundle（先 GET、改 ClassOverrides 字段、PUT 回去）。

- [ ] **Step 3: 给 redis 配 ClassOverrides[coe]**

```bash
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/redis" \
  -d '{
    "class_overrides": {
      "coe": {
        "REDIS_HOST": "10.37.6.235",
        "REDIS_PORT": "6380",
        "REDIS_PASSWORD": "<chiwei-test-redis-password>"
      }
    }
  }' | jq .
```

- [ ] **Step 4: 给 rabbitmq 配 ClassOverrides[coe]**

```bash
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/rabbitmq" \
  -d '{
    "class_overrides": {
      "coe": {
        "RABBITMQ_URL": "amqp://<user>:<password>@10.37.6.235:5673/"
      }
    }
  }' | jq .
```

- [ ] **Step 5: 给 lark-server-runtime 配 ClassOverrides[coe]**

```bash
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/lark-server-runtime" \
  -d '{
    "class_overrides": {
      "coe": {
        "SYNCHRONIZE_DB": "true"
      }
    }
  }' | jq .
```

- [ ] **Step 6: 验证 coe-validation lane 的 resolved-config**

```bash
for app in agent-service vectorize-worker lark-server recall-worker chat-response-worker; do
  echo "=== $app coe-validation ==="
  curl -sS -H "X-API-Key: $PAAS_TOKEN" "$PAAS_API/api/paas/apps/$app/resolved-config?lane=coe-validation" \
    | jq '.[] | select(.key | IN("POSTGRES_HOST","POSTGRES_PORT","POSTGRES_DB","REDIS_HOST","RABBITMQ_URL","SYNCHRONIZE_DB"))'
done
```

Expected:
- 5 个业务 App 的 POSTGRES_HOST 都是 10.37.6.235，source 含 `[class:coe]`
- REDIS_HOST 都是 10.37.6.235
- RABBITMQ_URL 指向 5673
- 3 个 lark-server 镜像 App 的 SYNCHRONIZE_DB=true，source `lark-server-runtime[class:coe]`
- 2 个 agent-service 镜像 App（agent-service / vectorize-worker）不含 SYNCHRONIZE_DB（它们没引用 lark-server-runtime）

- [ ] **Step 7: 给 4 个 bundle 加 RequiredKeys[coe]**

```bash
# pg-main
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/pg-main" \
  -d '{"required_keys": {"coe": ["POSTGRES_HOST","POSTGRES_PORT","POSTGRES_USER","POSTGRES_PASSWORD","POSTGRES_DB"]}}' | jq '.required_keys'

# redis
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/redis" \
  -d '{"required_keys": {"coe": ["REDIS_HOST","REDIS_PORT","REDIS_PASSWORD"]}}' | jq '.required_keys'

# rabbitmq
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/rabbitmq" \
  -d '{"required_keys": {"coe": ["RABBITMQ_URL"]}}' | jq '.required_keys'

# lark-server-runtime
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/lark-server-runtime" \
  -d '{"required_keys": {"coe": ["SYNCHRONIZE_DB"]}}' | jq '.required_keys'
```

Expected: 4 个 bundle 都设上 RequiredKeys[coe]

- [ ] **Step 8: prod 部署再次 smoke test（确认 RequiredKeys 不影响 prod）**

```bash
make deploy APP=agent-service LANE=prod GIT_REF=chore/enhance-dev-workflow
```

Expected: 部署成功（prod lane 走 LaneClassProd，coe RequiredKeys 不触发）

---

## Task 14: 配置 lark-proxy AllowedLaneClasses

- [ ] **Step 1: PATCH lark-proxy App 设 AllowedLaneClasses**

```bash
curl -sS -X PUT -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/apps/lark-proxy" \
  -d '{"allowed_lane_classes": ["prod"]}' | jq '.allowed_lane_classes'
```

Expected: 返回 `["prod"]`

- [ ] **Step 2: 验证 prod 部署仍然 work**

```bash
make deploy APP=lark-proxy LANE=prod GIT_REF=main
```

Expected: 部署成功

- [ ] **Step 3: 反向验证 — lark-proxy 部署到 coe-* 必 reject**

```bash
make deploy APP=lark-proxy LANE=coe-validation 2>&1 | tail -10
```

Expected: 部署失败，error 含 `lark-proxy` `only allowed in lane classes [prod]` 或类似 AllowedLaneClasses reject 信息（HTTP 400）

---

## Task 15: 端到端验证 + 反向验证

按 spec 验收标准全过一遍。

- [ ] **Step 1: 部署 agent-service + vectorize-worker 到 coe-validation**

```bash
make deploy APP=agent-service LANE=coe-validation GIT_REF=chore/enhance-dev-workflow
make deploy APP=vectorize-worker LANE=coe-validation GIT_REF=chore/enhance-dev-workflow
```

Expected: 两个 Deployment 都起来，pod ready

- [ ] **Step 2: 验证 agent-service pod 真连了测试 PG**

```bash
make logs APP=agent-service LANE=coe-validation SINCE=5m KEYWORD="POSTGRES\|chiwei-test\|10.37.6.235\|create_all\|CREATE TABLE" 2>&1 | head -30
```

Expected:
- log 含 connect 到 10.37.6.235:5433
- log 含 ensure_business_schema 触发的 CREATE TABLE 语句（17 张业务表）
- 无 connection error

- [ ] **Step 3: 验证 vectorize-worker pod 真连了测试 PG + 跑了 ensure_business_schema**

```bash
make logs APP=vectorize-worker LANE=coe-validation SINCE=5m KEYWORD="POSTGRES\|create_all\|CREATE TABLE\|ensure_business_schema" 2>&1 | head -30
```

Expected: vectorize-worker 也跑了 ensure_business_schema、连了 10.37.6.235:5433

- [ ] **Step 4: 部署 lark-server / recall-worker / chat-response-worker 到 coe-validation**

```bash
for app in lark-server recall-worker chat-response-worker; do
  make deploy APP=$app LANE=coe-validation GIT_REF=main
done
```

Expected: 三个 Deployment 起来

- [ ] **Step 5: 验证 lark-server 三个 Deployment 都跑了 TypeORM SYNCHRONIZE**

```bash
for app in lark-server recall-worker chat-response-worker; do
  echo "=== $app ==="
  make logs APP=$app LANE=coe-validation SINCE=5m KEYWORD="SYNCHRONIZE\|CREATE TABLE\|TypeORM" 2>&1 | head -20
done
```

Expected: 三个都有 TypeORM 自动 sync 的 CREATE TABLE 输出

- [ ] **Step 6: 反向验证 — ppe-* lane 不触发 create_all**

部署 agent-service 到 ppe-validation：

```bash
make deploy APP=agent-service LANE=ppe-validation GIT_REF=chore/enhance-dev-workflow
```

⚠️ 这里 agent-service 的 ppe-validation lane 部署后会拿到 prod PG 连接（ppe-* 走 baseline），但 ensure_business_schema 不应该触发 create_all。验证 log：

```bash
make logs APP=agent-service LANE=ppe-validation SINCE=3m KEYWORD="ensure_business_schema\|CREATE TABLE" 2>&1
```

Expected: 不出现 CREATE TABLE 输出（ppe-* 守门生效）

⚠️ 关键安全检查：ppe-validation 部署后立刻 undeploy，避免 ppe lane 长期占用 prod 资源做"灰度"无意义。

```bash
make undeploy APP=agent-service LANE=ppe-validation
```

- [ ] **Step 7: 反向验证 — RequiredKeys 校验生效**

故意删 pg-main 的 ClassOverrides[coe] 一个 key（比如 POSTGRES_DB），重新 deploy coe-validation 看是否 reject：

```bash
# 先备份当前 ClassOverrides
curl -sS -H "X-API-Key: $PAAS_TOKEN" "$PAAS_API/api/paas/config-bundles/pg-main" \
  | jq '.class_overrides' > /tmp/pg-main-class-overrides-backup.json

# 删 POSTGRES_DB
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/pg-main" \
  -d '{"class_overrides": {"coe": {"POSTGRES_HOST": "10.37.6.235", "POSTGRES_PORT": "5433", "POSTGRES_USER": "<u>", "POSTGRES_PASSWORD": "<p>"}}}'

# 试部署 coe-validation
make deploy APP=agent-service LANE=coe-validation 2>&1 | tail -10

# Expected: reject，error 含 "pg-main" "POSTGRES_DB" "ClassOverrides[coe][POSTGRES_DB]"

# 恢复 ClassOverrides
curl -sS -X PATCH -H "X-API-Key: $PAAS_TOKEN" -H "Content-Type: application/json" \
  "$PAAS_API/api/paas/config-bundles/pg-main" \
  -d "{\"class_overrides\": $(cat /tmp/pg-main-class-overrides-backup.json)}"

# 验证恢复成功
curl -sS -H "X-API-Key: $PAAS_TOKEN" "$PAAS_API/api/paas/config-bundles/pg-main" | jq '.class_overrides.coe | keys'
# Expected: ["POSTGRES_DB","POSTGRES_HOST","POSTGRES_PASSWORD","POSTGRES_PORT","POSTGRES_USER"]
```

- [ ] **Step 8: 反向验证 — lark-proxy 禁部署 coe（已在 Task 14 Step 3 验过，再跑一次确认）**

```bash
make deploy APP=lark-proxy LANE=coe-validation 2>&1 | tail -5
```

Expected: reject，error 含 `lark-proxy` `only allowed in lane classes`

- [ ] **Step 9: 飞书 dev bot 端到端**

绑 dev bot 到 coe-validation lane（按 CLAUDE.md e2e-testing 规范）：

```bash
# 用 /ops 工具：
# /ops bind TYPE=bot KEY=dev LANE=coe-validation
```

发一条简单消息给 dev bot，确认整链路：lark-proxy（prod）→ lark-server（coe-validation, 测试 PG/MQ/Redis）→ agent-service（coe-validation）→ chat-response-worker（coe-validation） → 飞书回复。

Expected: 收到 dev bot 回复，整链路用了测试基建（log 验证）。

- [ ] **Step 10: 写验收记录文档**

新建 `docs/superpowers/plans/2026-05-11-dev-workflow-v2-phase-2-verification.md`，模仿 Phase 1 verification 文档格式：

```markdown
# Phase 2 端到端验证

日期：<填写>
paas-engine version：<填写>
agent-service version：<填写>
lark-server version：<填写>

## ConfigBundle 配置

(列出 4 个 bundle 的 ClassOverrides + RequiredKeys 配置摘要)

## 端到端 case

| case | 期望 | 实际 |
|---|---|---|
| agent-service coe-validation 启动 | 连测试 PG, ensure_business_schema 跑 CREATE TABLE | ... |
| vectorize-worker coe-validation 启动 | 同上 | ... |
| lark-server / recall-worker / chat-response-worker coe-validation | TypeORM SYNCHRONIZE | ... |
| ppe-validation 不建表 | log 无 CREATE TABLE | ... |
| RequiredKeys 删 POSTGRES_DB → reject | HTTP 400 + error 明示缺 POSTGRES_DB | ... |
| lark-proxy → coe-* reject | HTTP 400 | ... |
| 飞书 dev bot → coe-validation 收回复 | 链路走测试基建 | ... |

## 已知 limitation

- 跨 coe-* lane 共享同一测试基建（Phase 5 隔离）
- 跨 service 重叠表 schema 漂移（已知 risk，spec 内）

## 清理
- ✅ undeploy agent-service / vectorize-worker / lark-server / recall-worker / chat-response-worker LANE=coe-validation
- ✅ undeploy ppe-validation
- ✅ /ops unbind dev bot
- chiwei-test 基建容器 + namespace 保留
```

- [ ] **Step 11: undeploy 所有验证 lane**

```bash
for app in agent-service vectorize-worker lark-server recall-worker chat-response-worker; do
  make undeploy APP=$app LANE=coe-validation
done

# /ops unbind TYPE=bot KEY=dev
```

Expected: 所有 coe-validation Deployment 删除

- [ ] **Step 12: Commit verification 文档**

```bash
git add docs/superpowers/plans/2026-05-11-dev-workflow-v2-phase-2-verification.md
git commit -m "docs(workflow): Phase 2 端到端验证记录

5 个业务 App + 反向验证全过：
- coe-validation lane 业务连测试基建、自动建表
- ppe-validation 不建表、不污染 prod
- RequiredKeys 校验生效（删 key 后 reject）
- lark-proxy AllowedLaneClasses 禁 coe-* 部署
- 飞书 dev bot 链路走测试基建

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 13: push 分支**

```bash
git push origin chore/enhance-dev-workflow
```

Phase 2 完成。下一步：用户决定 ship Phase 1+2 PR 还是继续 Phase 3。
