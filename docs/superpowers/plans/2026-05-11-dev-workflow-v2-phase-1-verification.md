# Phase 1 端到端验证

日期：2026-05-11
paas-engine version：1.0.0.49（commit `132587e`，含 lane 校验 + ErrInvalidInput wrap fix）
验证 lane：`ppe-lane-validation`（独立 paas-engine 实例，跟 prod paas-engine 完全隔离 process）

## 测试基建容器（cpu1 docker）

| 容器 | 状态 | 端口 | 验证命令输出 |
|---|---|---|---|
| chiwei-test-postgres | ✅ Up healthy | 5433 | `pg_isready` → `/var/run/postgresql:5432 - accepting connections` |
| chiwei-test-rabbitmq | ✅ Up healthy | 5673 / 15673 | `rabbitmq-diagnostics -q ping` → `Ping succeeded` |
| chiwei-test-redis | ✅ Up healthy | 6380 | `redis-cli ping` → `PONG` |

部署位置：cpu1 `~/chiwei-test-env/docker-compose.yaml`，env 文件 `~/.chiwei-test-env.env`（chmod 600）

## K8s namespace（k3s）

| 资源 | 状态 |
|---|---|
| namespace `chiwei-test` | ✅ Active，labels `chiwei.io/lane-class=coe,env=test,kubernetes.io/metadata.name=chiwei-test` |
| ResourceQuota `chiwei-test-quota` | ✅ requests: 0/8 cpu, 0/16Gi mem; limits: 0/16 cpu, 0/32Gi mem; pods 0/50; pvc 0/10 |
| NetworkPolicy `deny-cross-ns-egress` | ✅ created |

## paas-engine lane 校验（端到端 HTTP）

通过 `x-lane: ppe-lane-validation` header 把请求路由到含 lane 校验代码的 paas-engine 实例（pod `paas-engine-ppe-lane-validation-66869cd5cf-kmq8l`）。

| 测试用例 | 期望 | 实际 |
|---|---|---|
| `lane=feature-bad-name`（无前缀 reject） | HTTP 400 | ✅ `400 invalid input: lane "feature-bad-name" rejected: must match prod \| blue \| coe-<name> \| ppe-<name>` |
| `lane=ppe-test-accept` + 不存在 app（accept 路径不污染 DB） | HTTP 404 + `app not found` | ✅ `404 app not found`（lane 校验过、卡在 app lookup） |
| `lane=Coe-Foo`（大写 reject） | HTTP 400 | ✅ `400 invalid input: lane "Coe-Foo" rejected` |
| `lane=coe-`（前缀字面 reject） | HTTP 400 | ✅ `400 invalid input: lane "coe-" rejected` |

## 验证暴露的 bug + fix

第一轮验证 reject 路径返回 HTTP 500（应该 400）—— release_handler 通过 `errors.Is(err, domain.ErrInvalidInput)` 分发 status code，但 ClassifyLane 的 `fmt.Errorf` 没 wrap sentinel。

Fix（commit `132587e`）：
- `apps/paas-engine/internal/domain/lane.go` 两条 reject 分支都加 `%w: ErrInvalidInput` wrap，对齐 paas-engine 现有 `validate.go` 的模式
- 新增测试 `TestClassifyLane_ErrorWrapsInvalidInput` 强制覆盖 `errors.Is(err, ErrInvalidInput)`，防回归

部署 1.0.0.49 后所有 4 个 case 按期望表现。

## 已知跟进 / 推到 Phase 2-6

- 业务 SDK 切 dynamic config 路径（Phase 2 spec/plan 待写）
- dynamic config 翻译层按 lane 派 PG/MQ/Redis 连接串
- mock 飞书 / mock 外部 API services
- contract test runner + ship 门禁 + image_digest 绑定
- multi-lane 内部隔离（每 coe lane 独立 schema/vhost/redis prefix）+ lane 销毁自动清理
- 资源熔断 baseline（PG statement_timeout / RabbitMQ queue TTL / Redis key TTL）
- mock 契约漂移检测

## 清理

- ✅ `make undeploy APP=paas-engine LANE=ppe-lane-validation`（验证完毕清掉 ppe 测试 lane，避免占用资源）
- chiwei-test 基建容器 + namespace 保留（这是常态部署，给后续 Phase 2+ 业务部到 coe-* lane 时用）
