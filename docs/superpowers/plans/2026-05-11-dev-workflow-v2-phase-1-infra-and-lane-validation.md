# Dev Workflow v2 — Phase 1: 测试基建容器 + lane 校验

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 拉起测试基建（独立 PG / RabbitMQ / Redis 容器 + chiwei-test K8s namespace），并让 paas-engine 在 release create 时强制校验 lane 命名前缀（coe-* / ppe-* / 保留名 / 白名单），不通过 reject。

**Architecture:** 基础设施容器跑在 cpu1 宿主机 docker（跟现有 prod PG 同部署模式），通过 K8s headless Service 让 ns 内业务能解析。lane 校验逻辑加在 paas-engine 的 release create handler 里，新增 `domain/lane.go` 集中放前缀解析 + 白名单。

**Tech Stack:** Docker Compose（基础设施容器）、K8s YAML（namespace + ResourceQuota + NetworkPolicy）、Go 1.25（paas-engine）、Go testing（unit test）。

**Spec coverage（本 plan 覆盖 spec 的哪几段）：**
- ✅ §"测试基建（业务层独立）"中的 PG / RabbitMQ / Redis / K8s namespace 容器化部分
- ✅ §"lane 分类用命名前缀"全段
- ❌ Mock 飞书 / Mock 外部 API（Phase 3）
- ❌ paas-engine dynamic config 翻译层 + 业务 SDK 切 dynamic config（Phase 2）
- ❌ 多 coe lane 内部隔离 + 销毁清理（Phase 5）
- ❌ 上线门禁 framework 契约测试（Phase 4）
- ❌ Mock 契约漂移检测（Phase 6）
- ❌ 资源熔断 baseline（Phase 5）

---

## File Structure

新增文件：
- `infra/test-env/docker-compose.yaml`：chiwei-test-postgres / -rabbitmq / -redis 三个容器声明
- `infra/test-env/README.md`：基建拉起 / 验证 / 销毁的命令文档
- `infra/k8s/test-env/namespace.yaml`：chiwei-test ns + ResourceQuota + NetworkPolicy
- `apps/paas-engine/internal/domain/lane.go`：lane 类别 enum + 前缀解析 + 白名单
- `apps/paas-engine/internal/domain/lane_test.go`：lane 校验单测

修改文件：
- `apps/paas-engine/internal/service/release_service.go:43-71` (CreateOrUpdateRelease)：增加 lane 校验 hook，校验失败返回 error
- `apps/paas-engine/internal/service/release_service_test.go`：增加 lane 校验失败的 release 创建测试

---

## Task 1: chiwei-test-postgres docker 容器

**Files:**
- Create: `infra/test-env/docker-compose.yaml`
- Create: `infra/test-env/README.md`

- [ ] **Step 1: 写 compose 文件**

`infra/test-env/docker-compose.yaml`:

```yaml
version: "3.9"
services:
  chiwei-test-postgres:
    image: postgres:16-alpine
    container_name: chiwei-test-postgres
    restart: unless-stopped
    environment:
      POSTGRES_DB: chiwei_test
      POSTGRES_USER: chiwei_test
      POSTGRES_PASSWORD: ${CHIWEI_TEST_PG_PASSWORD:?password required}
    ports:
      - "5433:5432"  # prod PG 占 5432，test 用 5433
    volumes:
      - chiwei_test_pg_data:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U chiwei_test -d chiwei_test"]
      interval: 10s
      timeout: 5s
      retries: 5

volumes:
  chiwei_test_pg_data:
    name: chiwei_test_pg_data
```

- [ ] **Step 2: 写 README 拉起命令**

`infra/test-env/README.md`:

````markdown
# 测试环境基础设施

跑在 cpu1 宿主机 docker 上，跟现有 prod PG 同部署模式。

## 拉起

```bash
cd infra/test-env
export CHIWEI_TEST_PG_PASSWORD=<set-a-password>
docker compose up -d chiwei-test-postgres
```

## 验证

```bash
docker exec chiwei-test-postgres pg_isready -U chiwei_test -d chiwei_test
# 期望: localhost:5432 - accepting connections

docker exec chiwei-test-postgres psql -U chiwei_test -d chiwei_test -c '\dt'
# 期望: Did not find any relations.
```

## 销毁

```bash
docker compose down chiwei-test-postgres
docker volume rm chiwei_test_pg_data  # 慎用，会丢测试数据
```
````

- [ ] **Step 3: 在 cpu1 上拉起验证**

```bash
ssh cpu1 'cd ~/chiwei-platform/infra/test-env && export CHIWEI_TEST_PG_PASSWORD=test123 && docker compose up -d chiwei-test-postgres'
```

期望输出：
```
[+] Running 2/2
 ✔ Volume "chiwei_test_pg_data"        Created
 ✔ Container chiwei-test-postgres      Started
```

- [ ] **Step 4: 验证容器健康**

```bash
ssh cpu1 'docker exec chiwei-test-postgres pg_isready -U chiwei_test -d chiwei_test'
```

期望：`/var/run/postgresql:5432 - accepting connections`

```bash
ssh cpu1 'docker exec chiwei-test-postgres psql -U chiwei_test -d chiwei_test -c "SELECT version();"'
```

期望返回 PostgreSQL 16.x 版本字符串。

- [ ] **Step 5: Commit**

```bash
git add infra/test-env/docker-compose.yaml infra/test-env/README.md
git commit -m "feat(test-env): chiwei-test-postgres 独立 docker 容器

跑在 cpu1，端口 5433（避开 prod PG 5432），独立卷 chiwei_test_pg_data。
独立实例的理由见 spec：契约测试可能制造大事务/大 WAL/磁盘打满，schema
级隔离兜不住实例级故障。"
```

---

## Task 2: chiwei-test-rabbitmq docker 容器

**Files:**
- Modify: `infra/test-env/docker-compose.yaml`（追加 service）
- Modify: `infra/test-env/README.md`（追加验证命令）

- [ ] **Step 1: 在 compose 文件追加 rabbitmq service**

在 `services:` 块下追加：

```yaml
  chiwei-test-rabbitmq:
    image: rabbitmq:3.13-management-alpine
    container_name: chiwei-test-rabbitmq
    restart: unless-stopped
    environment:
      RABBITMQ_DEFAULT_USER: chiwei_test
      RABBITMQ_DEFAULT_PASS: ${CHIWEI_TEST_MQ_PASSWORD:?password required}
    ports:
      - "5673:5672"      # AMQP（prod 占 5672，test 用 5673）
      - "15673:15672"    # Management UI
    volumes:
      - chiwei_test_mq_data:/var/lib/rabbitmq
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
```

在 `volumes:` 块下追加：

```yaml
  chiwei_test_mq_data:
    name: chiwei_test_mq_data
```

- [ ] **Step 2: README 追加 rabbitmq 验证命令**

```bash
docker exec chiwei-test-rabbitmq rabbitmq-diagnostics -q ping
# 期望: Ping succeeded

# 浏览器开 http://cpu1:15673 用 chiwei_test / <password> 登录 management UI
```

- [ ] **Step 3: 在 cpu1 上拉起验证**

```bash
ssh cpu1 'cd ~/chiwei-platform/infra/test-env && export CHIWEI_TEST_MQ_PASSWORD=test123 && docker compose up -d chiwei-test-rabbitmq'
ssh cpu1 'docker exec chiwei-test-rabbitmq rabbitmq-diagnostics -q ping'
```

期望最后一行：`Ping succeeded`

- [ ] **Step 4: Commit**

```bash
git add infra/test-env/docker-compose.yaml infra/test-env/README.md
git commit -m "feat(test-env): chiwei-test-rabbitmq 独立 docker 容器

3.13-management 镜像（带 management UI），AMQP 5673 / UI 15673。独立
实例的理由跟 PG 一致：memory watermark / disk alarm / connection 风暴
是实例级，跨 vhost 共享。"
```

---

## Task 3: chiwei-test-redis docker 容器

**Files:**
- Modify: `infra/test-env/docker-compose.yaml`（追加 service）
- Modify: `infra/test-env/README.md`

- [ ] **Step 1: 在 compose 追加 redis service**

```yaml
  chiwei-test-redis:
    image: redis:7-alpine
    container_name: chiwei-test-redis
    restart: unless-stopped
    command:
      - redis-server
      - --requirepass
      - ${CHIWEI_TEST_REDIS_PASSWORD:?password required}
      - --maxmemory
      - 512mb
      - --maxmemory-policy
      - allkeys-lru
    ports:
      - "6380:6379"  # prod redis 占 6379，test 用 6380
    volumes:
      - chiwei_test_redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "-a", "${CHIWEI_TEST_REDIS_PASSWORD}", "ping"]
      interval: 10s
      timeout: 5s
      retries: 5
```

`volumes:` 追加：

```yaml
  chiwei_test_redis_data:
    name: chiwei_test_redis_data
```

- [ ] **Step 2: README 追加 redis 验证命令**

```bash
docker exec chiwei-test-redis redis-cli -a $CHIWEI_TEST_REDIS_PASSWORD ping
# 期望: PONG
```

- [ ] **Step 3: 在 cpu1 上拉起验证**

```bash
ssh cpu1 'cd ~/chiwei-platform/infra/test-env && export CHIWEI_TEST_REDIS_PASSWORD=test123 && docker compose up -d chiwei-test-redis'
ssh cpu1 'docker exec chiwei-test-redis redis-cli -a test123 ping'
```

期望最后一行：`PONG`

- [ ] **Step 4: Commit**

```bash
git add infra/test-env/docker-compose.yaml infra/test-env/README.md
git commit -m "feat(test-env): chiwei-test-redis 独立 docker 实例

不用 select db 隔离（多 db 是 antirez 自己说不推荐的历史遗留）。
512mb maxmemory + allkeys-lru，避免测试数据撑爆。"
```

---

## Task 4: chiwei-test K8s namespace + ResourceQuota + NetworkPolicy

**Files:**
- Create: `infra/k8s/test-env/namespace.yaml`

- [ ] **Step 1: 写 namespace + quota + netpol**

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: chiwei-test
  labels:
    env: test
    chiwei.io/lane-class: coe
---
apiVersion: v1
kind: ResourceQuota
metadata:
  name: chiwei-test-quota
  namespace: chiwei-test
spec:
  hard:
    requests.cpu: "8"
    requests.memory: 16Gi
    limits.cpu: "16"
    limits.memory: 32Gi
    pods: "50"
    persistentvolumeclaims: "10"
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-cross-ns-egress
  namespace: chiwei-test
spec:
  podSelector: {}
  policyTypes:
    - Egress
  egress:
    # 允许：同 ns 内
    - to:
        - namespaceSelector:
            matchLabels:
              env: test
    # 允许：DNS（kube-system）
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
    # 允许：访问 cpu1 宿主机（chiwei-test-postgres / -rabbitmq / -redis）
    - to:
        - ipBlock:
            cidr: 10.37.6.235/32  # cpu1 IP
```

- [ ] **Step 2: apply 到 k3s**

```bash
ssh cpu1 'kubectl apply -f ~/chiwei-platform/infra/k8s/test-env/namespace.yaml'
```

期望：
```
namespace/chiwei-test created
resourcequota/chiwei-test-quota created
networkpolicy.networking.k8s.io/deny-cross-ns-egress created
```

- [ ] **Step 3: 验证 namespace 和 quota**

```bash
ssh cpu1 'kubectl get ns chiwei-test --show-labels'
```

期望含 `env=test,chiwei.io/lane-class=coe`。

```bash
ssh cpu1 'kubectl get resourcequota -n chiwei-test'
```

期望：列出 chiwei-test-quota，hard 资源限额匹配。

```bash
ssh cpu1 'kubectl get networkpolicy -n chiwei-test'
```

期望：列出 deny-cross-ns-egress。

- [ ] **Step 4: Commit**

```bash
git add infra/k8s/test-env/namespace.yaml
git commit -m "feat(test-env): chiwei-test K8s namespace + quota + netpol

namespace label env=test/chiwei.io/lane-class=coe 用于后续 prom rule
排除告警。ResourceQuota 限 CPU 8/16 cores、内存 16/32GB、Pod 50 个，
防止测试抢 prod 资源。NetworkPolicy 限制 egress 只能到同 ns / kube-dns
/ cpu1 宿主机（拿 test PG/MQ/Redis），断掉跨 ns 误访问 prod。"
```

---

## Task 5: paas-engine 新增 lane 类别识别 domain

**Files:**
- Create: `apps/paas-engine/internal/domain/lane.go`
- Create: `apps/paas-engine/internal/domain/lane_test.go`

- [ ] **Step 1: 先写测试**

`apps/paas-engine/internal/domain/lane_test.go`:

```go
package domain

import "testing"

func TestClassifyLane(t *testing.T) {
	cases := []struct {
		name      string
		lane      string
		whitelist []string
		want      LaneClass
		wantErr   bool
	}{
		{name: "prod 保留名", lane: "prod", want: LaneClassProd},
		{name: "blue 保留名", lane: "blue", want: LaneClassProd},
		{name: "coe 前缀", lane: "coe-test-1", want: LaneClassCoe},
		{name: "ppe 前缀", lane: "ppe-canary", want: LaneClassPpe},
		{name: "coe 前缀但只有前缀字面 reject", lane: "coe-", wantErr: true},
		{name: "ppe 前缀但只有前缀字面 reject", lane: "ppe-", wantErr: true},
		{name: "无前缀 reject", lane: "feature-x", wantErr: true},
		{name: "无前缀 reject (sandbox)", lane: "sandbox", wantErr: true},
		{name: "白名单兼容 dev", lane: "dev", whitelist: []string{"dev"}, want: LaneClassProd},
		{name: "白名单不在 reject", lane: "weird-old-lane", whitelist: []string{"dev"}, wantErr: true},
		{name: "空 lane reject", lane: "", wantErr: true},
		{name: "大写 reject (强制小写)", lane: "Coe-Foo", wantErr: true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := ClassifyLane(tc.lane, tc.whitelist)
			if tc.wantErr {
				if err == nil {
					t.Fatalf("expected error, got class=%v", got)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if got != tc.want {
				t.Fatalf("class=%v, want=%v", got, tc.want)
			}
		})
	}
}
```

- [ ] **Step 2: 跑测试看到 fail**

```bash
cd apps/paas-engine && go test ./internal/domain/ -run TestClassifyLane -v
```

期望：FAIL with `undefined: ClassifyLane` / `undefined: LaneClass*`

- [ ] **Step 3: 实现 lane.go**

`apps/paas-engine/internal/domain/lane.go`:

```go
package domain

import (
	"fmt"
	"regexp"
	"slices"
)

// LaneClass 表示 lane 的环境类别。fail-closed：未知类别一律 reject。
type LaneClass int

const (
	LaneClassUnknown LaneClass = iota
	LaneClassProd              // prod / blue / 历史白名单：连 prod 基建
	LaneClassCoe               // coe-*：连测试基建
	LaneClassPpe               // ppe-*：连 prod 基建（灰度/AB）
)

func (c LaneClass) String() string {
	switch c {
	case LaneClassProd:
		return "prod"
	case LaneClassCoe:
		return "coe"
	case LaneClassPpe:
		return "ppe"
	default:
		return "unknown"
	}
}

// 强制 lowercase + 字母数字+ - 之后必须有非空字符。
var coePattern = regexp.MustCompile(`^coe-[a-z0-9][a-z0-9-]*$`)
var ppePattern = regexp.MustCompile(`^ppe-[a-z0-9][a-z0-9-]*$`)

// 保留名（paas-engine 蓝绿专用，等同 prod 基建）。
var reservedNames = []string{"prod", "blue"}

// ClassifyLane 用 fail-closed 语义解析 lane 类别。
//   - prod / blue：保留名，返回 LaneClassProd
//   - coe-* / ppe-*：合法前缀，返回对应类别
//   - whitelist 内：兼容历史 lane，按 LaneClassProd 处理（白名单有过期日期，调用方传入）
//   - 其他：返回 error，caller 必须 reject
func ClassifyLane(lane string, whitelist []string) (LaneClass, error) {
	if lane == "" {
		return LaneClassUnknown, fmt.Errorf("lane name is empty")
	}
	if slices.Contains(reservedNames, lane) {
		return LaneClassProd, nil
	}
	if slices.Contains(whitelist, lane) {
		return LaneClassProd, nil
	}
	if coePattern.MatchString(lane) {
		return LaneClassCoe, nil
	}
	if ppePattern.MatchString(lane) {
		return LaneClassPpe, nil
	}
	return LaneClassUnknown, fmt.Errorf(
		"lane %q rejected: must match prod | blue | coe-<name> | ppe-<name>; got no recognized prefix",
		lane,
	)
}
```

- [ ] **Step 4: 跑测试验证全过**

```bash
cd apps/paas-engine && go test ./internal/domain/ -run TestClassifyLane -v
```

期望：全部 12 个 case PASS。

- [ ] **Step 5: Commit**

```bash
git add apps/paas-engine/internal/domain/lane.go apps/paas-engine/internal/domain/lane_test.go
git commit -m "feat(paas-engine): 新增 lane 类别识别 domain

ClassifyLane 用 fail-closed 语义：
- prod / blue 保留名 → LaneClassProd
- coe-<name> → LaneClassCoe（连测试基建）
- ppe-<name> → LaneClassPpe（连 prod 基建）
- 历史 lane 走显式白名单 → LaneClassProd
- 其他无前缀 lane 一律 reject

正则强制 lowercase + 前缀后非空字符（避免 'coe-' 'Coe-Foo' 这种）。"
```

---

## Task 6: release_service 集成 lane 校验

**Files:**
- Modify: `apps/paas-engine/internal/service/release_service.go:43-71` (CreateOrUpdateRelease)
- Create: `apps/paas-engine/internal/service/release_service_test.go`（如果已存在则修改）

- [ ] **Step 1: 先看现状代码**

```bash
sed -n '40,90p' apps/paas-engine/internal/service/release_service.go
```

记下 CreateOrUpdateRelease 的签名、参数结构、错误返回模式（用于下面写测试和实现匹配现有风格）。

- [ ] **Step 2: 写测试 — lane 校验失败时 release 创建被 reject**

`apps/paas-engine/internal/service/release_service_test.go`（追加测试或新建）：

```go
package service

import (
	"context"
	"strings"
	"testing"

	"github.com/chiwei/paas-engine/internal/domain"
)

func TestCreateOrUpdateRelease_RejectsBadLaneName(t *testing.T) {
	svc := newTestReleaseService(t) // 已有 helper，若没有则用 mock repo 构造

	_, err := svc.CreateOrUpdateRelease(context.Background(), domain.CreateReleaseRequest{
		AppName: "agent-service",
		Lane:    "feature-x", // 无前缀，应 reject
		Image:   "harbor.local:30002/inner-bot/agent-service:1.0.0.1",
	})

	if err == nil {
		t.Fatal("expected lane validation error, got nil")
	}
	if !strings.Contains(err.Error(), "lane") {
		t.Fatalf("error should mention 'lane', got: %v", err)
	}
}

func TestCreateOrUpdateRelease_AcceptsValidLanes(t *testing.T) {
	cases := []string{"prod", "blue", "coe-test-1", "ppe-canary"}
	for _, lane := range cases {
		t.Run(lane, func(t *testing.T) {
			svc := newTestReleaseService(t)
			_, err := svc.CreateOrUpdateRelease(context.Background(), domain.CreateReleaseRequest{
				AppName: "agent-service",
				Lane:    lane,
				Image:   "harbor.local:30002/inner-bot/agent-service:1.0.0.1",
			})
			if err != nil && strings.Contains(err.Error(), "lane") {
				t.Fatalf("lane %q should pass validation but got: %v", lane, err)
			}
		})
	}
}
```

> 备注：`newTestReleaseService` 是测试 helper。如果 release_service 还没有测试 helper，先写一个最小版本：mock 出 ReleaseRepository、Deployer 等依赖只让 CreateOrUpdateRelease 能跑到 lane 校验那一步即可（其他副作用 mock 成 noop）。

- [ ] **Step 3: 跑测试看到 fail（校验还没加）**

```bash
cd apps/paas-engine && go test ./internal/service/ -run TestCreateOrUpdateRelease_RejectsBadLaneName -v
```

期望：FAIL（lane=feature-x 当前会被接受）。

- [ ] **Step 4: 在 CreateOrUpdateRelease 加校验**

读 `apps/paas-engine/internal/service/release_service.go` 找到 CreateOrUpdateRelease 函数体起始（line 43 附近）。在第一行业务逻辑前插入：

```go
// lane 命名前缀强制校验（fail-closed）—— spec: dev-workflow-v2 §"lane 分类用命名前缀"
laneWhitelist := s.cfg.LegacyLaneWhitelist // []string，从 paas-engine config 读
if _, err := domain.ClassifyLane(req.Lane, laneWhitelist); err != nil {
    return nil, fmt.Errorf("release create rejected: %w", err)
}
```

如果 service struct 里没有 cfg 字段或 LegacyLaneWhitelist 还没加，先在 ReleaseService struct + 构造函数 + paas-engine config 里加：

`apps/paas-engine/internal/service/release_service.go`（struct 定义处）：

```go
type ReleaseService struct {
    // ... 现有字段
    cfg ReleaseServiceConfig
}

type ReleaseServiceConfig struct {
    LegacyLaneWhitelist []string // 从 env / config 读，例如 ["dev"]
}
```

paas-engine 主 config 里加对应字段（具体路径根据现有 config 模型，找 `apps/paas-engine/internal/config/` 或 `apps/paas-engine/main.go` 的 cfg load 处）：

```go
// 例如 internal/config/config.go
type Config struct {
    // ...
    LegacyLaneWhitelist []string `env:"LEGACY_LANE_WHITELIST" envSeparator:","`
}
```

构造 ReleaseService 处把 cfg 传进去。

- [ ] **Step 5: 跑测试验证 reject 通过、合法 lane 通过**

```bash
cd apps/paas-engine && go test ./internal/service/ -run "TestCreateOrUpdateRelease_(RejectsBadLaneName|AcceptsValidLanes)" -v
```

期望：两个测试 + 4 个子测试全 PASS。

- [ ] **Step 6: 跑全 paas-engine 测试看没 regress**

```bash
cd apps/paas-engine && go test ./... -count=1
```

期望：全 PASS（如果有 release 相关老测试用 lane="dev" 之类的，可能要把 LegacyLaneWhitelist 测试 helper 默认加 ["dev"]）。

- [ ] **Step 7: Commit**

```bash
git add apps/paas-engine/internal/service/release_service.go apps/paas-engine/internal/service/release_service_test.go apps/paas-engine/internal/config/config.go
git commit -m "feat(paas-engine): release create 强制校验 lane 命名前缀

CreateOrUpdateRelease 第一步调 domain.ClassifyLane，违反前缀规则的
lane 直接 reject，error 透传到 HTTP 400。

历史遗留 lane（dev 等）走 LEGACY_LANE_WHITELIST env var 显式白名单
兼容，过期清掉。

Spec ref: docs/superpowers/specs/2026-05-11-dev-workflow-v2-test-env-isolation-design.md §lane 分类用命名前缀"
```

---

## Task 7: 端到端验证 — lane 校验真生效

**Files:** （无新文件，纯 paas-engine 端口验证）

- [ ] **Step 1: 部署 paas-engine 蓝绿到测试**

按 CLAUDE.md "make self-deploy" 流程把当前 paas-engine commit 部到 blue lane 验证：

```bash
make self-deploy
```

期望最后输出 `Released to blue successfully` 或类似（依现有 Makefile 输出）。

- [ ] **Step 2: 试着创建一个无前缀 lane 的 release，期望 400**

```bash
# api-test skill 的 http.sh 脚本
.claude/skills/api-test/scripts/http.sh POST "$PAAS_API/api/paas/releases/" \
  '{"app_name":"agent-service","lane":"feature-bad-name","image_tag":"harbor.local:30002/inner-bot/agent-service:1.0.0.1"}' \
  "X-API-Key: $PAAS_API_KEY"
```

期望返回：HTTP 400，error 消息含 `lane "feature-bad-name" rejected`。

- [ ] **Step 3: 试着创建一个 coe-* lane 的 release，期望 200**

```bash
.claude/skills/api-test/scripts/http.sh POST "$PAAS_API/api/paas/releases/" \
  '{"app_name":"agent-service","lane":"coe-validate-1","image_tag":"harbor.local:30002/inner-bot/agent-service:1.0.0.1"}' \
  "X-API-Key: $PAAS_API_KEY"
```

期望返回 200，release 记录创建成功。

> 注意：本 task 只验证"lane 校验工作"，coe-validate-1 实际部署会走 K8s deployment（可能需要清理）。验证完用：

```bash
make undeploy APP=agent-service LANE=coe-validate-1
```

- [ ] **Step 4: 验证 prod / blue / 白名单 lane 通过**

```bash
# prod 通过（已有逻辑）
.claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/releases/?app_name=agent-service&lane=prod" "X-API-Key: $PAAS_API_KEY"
# 期望：返回 prod 当前 release，证明 prod lane 没被新校验误伤

# 白名单 dev 通过（前提：LEGACY_LANE_WHITELIST=dev 已配）
.claude/skills/api-test/scripts/http.sh GET "$PAAS_API/api/paas/releases/?app_name=agent-service&lane=dev" "X-API-Key: $PAAS_API_KEY"
# 期望：返回 dev 当前 release（如果有），证明白名单生效
```

- [ ] **Step 5: 把验证结果写到一个简短 verification.md**

`docs/superpowers/plans/2026-05-11-dev-workflow-v2-phase-1-verification.md`：

````markdown
# Phase 1 端到端验证

日期：<填上>
paas-engine commit/version：<填上>

## 测试基建容器

- chiwei-test-postgres：5433 端口可达，pg_isready 通过 ✅ / ❌
- chiwei-test-rabbitmq：5673 + 15673 端口可达，diagnostics ping 通过 ✅ / ❌
- chiwei-test-redis：6380 端口可达，PONG 返回 ✅ / ❌

## K8s namespace

- chiwei-test ns 存在，labels env=test ✅ / ❌
- ResourceQuota 生效（kubectl describe 看到 hard limit）✅ / ❌
- NetworkPolicy 存在 ✅ / ❌

## paas-engine lane 校验

- 无前缀 `feature-bad-name` 创建 release：HTTP 400 + error 含 "lane rejected" ✅ / ❌
- `coe-validate-1` 创建 release：HTTP 200 ✅ / ❌
- `prod` 创建/查询：HTTP 200 ✅ / ❌
- 白名单 `dev`（LEGACY_LANE_WHITELIST 含 dev）：HTTP 200 ✅ / ❌

## 已知问题 / 跟进

<列出验证发现的问题，移到 Phase 2 plan>
````

填好后 commit：

```bash
git add docs/superpowers/plans/2026-05-11-dev-workflow-v2-phase-1-verification.md
git commit -m "docs(verification): Phase 1 端到端验证记录"
```

- [ ] **Step 6: 通知用户 Phase 1 完成**

向用户报告：基建拉起 + namespace 建立 + lane 校验生效，附验证文档路径。等用户决定 Phase 2 是否启动。

---

## Self-Review checkpoints

跑完所有 task 之前，对 plan 做最后一次自查：

1. **Spec coverage**：本 plan 7 个 task 覆盖了 spec 的 §"测试基建（业务层独立）"PG/MQ/Redis 容器化部分 + §"lane 分类用命名前缀"全段。其他 spec 段落明确推到 Phase 2-6（已在 plan 头部列出）。
2. **Placeholder scan**：没有 TBD / TODO / "类似 Task N" / "适当处理错误"等含糊表达。每个代码 step 含完整代码块。
3. **Type consistency**：`LaneClass` enum / `ClassifyLane` 函数名 / `LegacyLaneWhitelist` 字段名在所有 task 一致。
4. **依赖顺序**：Task 1-3 之间独立可并行；Task 4 独立；Task 5-6 顺序依赖（5 写 domain、6 集成 service）；Task 7 依赖 5-6 真上线。

## Phase 2 启动条件

- Phase 1 验证文档全部 ✅
- 用户拍板进入 Phase 2
- Phase 2 范围预告：业务 SDK 切 dynamic config 路径 + paas-engine dynamic config 翻译层按 lane 派 PG/MQ/Redis 连接串 + 翻译层 fail-closed 校验
