# Dev Workflow v2 — Phase 3 Design: Qdrant / Mongo 基建外延 + Phase 2 漏 commit 修复

**日期**: 2026-05-12
**作者**: bezhai + Claude
**前置**: PR #218（Phase 1+2 已 ship 到 prod paas-engine 1.0.0.52）
**关联**: `docs/superpowers/specs/2026-05-11-dev-workflow-v2-test-env-isolation-design.md`（总 spec）

## 1. 背景

Phase 1+2 ship 后，端到端验证 coe-validation lane 部署 agent-service 时发现：除了 PG / Redis / RabbitMQ 这三件被 Phase 2 ClassOverrides 隔离的基建外，业务还**真实写入** prod 的其他状态化基建——最严重的是 agent-service 启动 `init_collections()` 直接打 prod Qdrant 建/复用 collection。

Phase 2 spec 范围只覆盖 PG/Redis/MQ + 一个 lark-server-runtime bundle，未盘点完业务实际依赖的全部 ConfigBundle。Phase 3 要把"业务能在 coe-* lane 真跑通而不污染 prod 内部基建"补完。

外部第三方 SaaS（阿里云 OSS / 火山 TOS / Langfuse / OpenAI/Azure/Google API）**不在本 Phase 范围**，作为 known limitation 显式声明（见 §6）。

## 2. 范围

**In scope**：

- 类 A 内部基建外延：**Qdrant**（agent-service / vectorize-worker 用） + **Mongo**（lark-server / chat-response-worker 用）独立测试容器 + ClassOverrides[coe] + RequiredKeys[coe]
- Phase 2 漏 commit 修复：`infra/test-env/docker-compose.yaml` 的 rabbitmq image 改动同步进 git

**Out of scope**（known limitation，§6 详述）：

- 外部 SaaS 不隔离：OSS（阿里云 ali-oss + 火山 TOS）、Langfuse、AI Provider API（OpenAI/Azure/Google）
- mock 服务（mock-feishu / mock-llm 等）
- paas-engine 多 namespace 部署支持
- lite-registry 多 namespace watch

## 3. 设计

### 3.1 测试容器（cpu1 docker-compose 追加）

`infra/test-env/docker-compose.yaml` 新增两个 service：

**chiwei-test-qdrant**:
- image: `qdrant/qdrant:latest`
- ports: `16333:6333`（HTTP REST API）/ `16334:6334`（gRPC）
- env: `QDRANT__SERVICE__API_KEY=chiwei-test-qdrant-key`
- volumes: 持久化 storage（参考 chiwei-test-postgres 数据目录约定）

**chiwei-test-mongo**:
- image: `mongo:7-alpine`
- ports: `27018:27017`
- env: `MONGO_INITDB_ROOT_USERNAME=chiwei-test` / `MONGO_INITDB_ROOT_PASSWORD=chiwei-test-mongo-pwd`
- volumes: 持久化 data 目录

**安全边界**（沿用 Phase 1 chiwei-test-postgres / -rabbitmq 同款）：cpu1 docker bridge 网络 + k3s prod ns 可达；不暴露公网；弱密码可接受。

### 3.2 ConfigBundle 设计

新建两个 bundle，跟 `pg-main` / `redis` / `rabbitmq` / `lark-server-runtime` 平级（不合并到现有 *-runtime bundle —— 沿用 Phase 1+2 一基建一 bundle 模板）。

**`qdrant` bundle**：

| 字段 | 值 |
|---|---|
| keys (baseline) | `QDRANT_SERVICE_HOST` / `QDRANT_SERVICE_PORT` / `QDRANT_SERVICE_API_KEY` = prod 现值 |
| `class_overrides[coe]` | `QDRANT_SERVICE_HOST=10.37.6.235` / `QDRANT_SERVICE_PORT=16333` / `QDRANT_SERVICE_API_KEY=chiwei-test-qdrant-key` |
| `required_keys[coe]` | `["QDRANT_SERVICE_HOST", "QDRANT_SERVICE_PORT", "QDRANT_SERVICE_API_KEY"]` |

**`mongo` bundle**：

| 字段 | 值 |
|---|---|
| keys (baseline) | `MONGO_HOST` / `MONGO_INITDB_ROOT_USERNAME` / `MONGO_INITDB_ROOT_PASSWORD` = prod 现值 |
| `class_overrides[coe]` | `MONGO_HOST=10.37.6.235:27018` / `MONGO_INITDB_ROOT_USERNAME=chiwei-test` / `MONGO_INITDB_ROOT_PASSWORD=chiwei-test-mongo-pwd` |
| `required_keys[coe]` | `["MONGO_HOST", "MONGO_INITDB_ROOT_USERNAME", "MONGO_INITDB_ROOT_PASSWORD"]` |

**MONGO_HOST 含 port 已验证**：`apps/lark-server/src/infrastructure/dal/mongo/client.ts` 拼 url `mongodb://${user}:${pwd}@${host}/chiwei?authSource=admin`——`${host}` 直接进 url 的 host 段，mongodb url 标准支持 `host:port` 形式，因此 `MONGO_HOST=10.37.6.235:27018` 直接生效，**无需 split key**。注意 client 写死 db 名 `chiwei` 和 `authSource=admin`，因此 chiwei-test-mongo 容器 root user 必须在 admin db（mongo 镜像默认行为）+ 业务首次写入会自动创建 `chiwei` db。

### 3.3 App `config_bundles` 引用追加

| Deployment | 镜像 | 追加 bundle |
|---|---|---|
| agent-service | agent-service | + `qdrant` |
| vectorize-worker | agent-service | + `qdrant` |
| lark-server | lark-server | + `mongo` |
| chat-response-worker | lark-server | + `mongo` |
| recall-worker | lark-server | （不动，不用 qdrant/mongo） |

引用方式：调用 paas-engine `PUT /api/paas/apps/{app}` 把现有 `config_bundles` 数组追加。

### 3.4 Phase 2 漏 commit 修复

Phase 2 实际把 cpu1 chiwei-test-rabbitmq 切到 `harbor.local:30002/inner-bot/rabbitmq:latest`（含 `rabbitmq_delayed_message_exchange` plugin），但 `infra/test-env/docker-compose.yaml` 仓库版本未 commit。

Phase 3 PR 同步把这个改动 commit 进 git，避免"fresh setup 新机器"语义漂移。

### 3.5 业务代码改动

**零业务代码改动**。Qdrant client 直接读 env 不依赖 ConfigBundle 抽象；Mongo client 拼接逻辑（`MongoClient` url）兼容 `host:port` 形态 MONGO_HOST。Phase 3 全部改动集中在：

- `infra/test-env/docker-compose.yaml`（基建容器配置）
- paas-engine ConfigBundle PUT 操作（数据，非代码）
- paas-engine App `config_bundles` 字段 PUT 操作（数据）

## 4. 实施顺序

1. 改 `infra/test-env/docker-compose.yaml`（新增 qdrant + mongo service，rabbitmq image 切 harbor，commit）
2. cpu1 拉起两个新容器（`docker-compose up -d`）+ 健康检查
3. 通过 paas-engine API 新建 `qdrant` + `mongo` bundle（含 baseline + ClassOverrides[coe] + RequiredKeys[coe]）
4. 通过 paas-engine API 给 4 个 App 追加 bundle 引用
5. 烟雾测试：
   - prod lane resolve 不变（`GET /api/paas/apps/agent-service/resolved-config?lane=prod` 跟改动前比对 byte-equal）
   - coe-validation lane resolve 真派 chiwei-test endpoint
6. 端到端：部 agent-service + vectorize-worker + lark-server + chat-response-worker + recall-worker 到 coe-validation lane
7. 验证：
   - agent-service 启动 log 含 `Creating collection messages_recall on http://10.37.6.235:16333` 类似
   - chiwei-test-qdrant 真有 4 个 collection
   - chiwei-test-mongo `db.lark_event` 真有 event 数据
   - prod qdrant / mongo 零变化（对比改动前 collection 列表 + lark_event count）
8. PR 发到 main，无 prod 行为变化

## 5. 验收（成功标准）

| 验收项 | 验证方式 | 期望 |
|---|---|---|
| 新 bundle CRUD 正常 | `GET /api/paas/config-bundles/qdrant` + `mongo` | 200 + 含 ClassOverrides + RequiredKeys |
| ClassOverrides 真生效 | `GET /api/paas/apps/agent-service/resolved-config?lane=coe-validation` | `QDRANT_SERVICE_HOST=10.37.6.235`，source `qdrant[class:coe]` |
| RequiredKeys 反向 reject | 故意删 bundle ClassOverrides[coe] 某 key 后 deploy coe lane | HTTP 400 |
| init_collections 真打测试 Qdrant | agent-service coe lane 部署后 log + `curl http://10.37.6.235:16333/collections` | 4 个 collection 真建 |
| insertEvent 真写测试 Mongo | lark-server coe lane 部署 + 飞书 event → `ssh cpu1 docker exec chiwei-test-mongo mongosh --eval 'db.lark_event.countDocuments()'` | count > 0 |
| prod 零变化 | resolved-config prod lane diff | byte-equal |

## 6. Known limitation（不在 Phase 3 范围）

**外部 SaaS / 第三方服务在 coe-* lane 仍指 prod**，本 Phase 不解决：

| 服务 | 现状 | coe lane 行为 |
|---|---|---|
| 阿里云 OSS (ali-oss SDK) | prod env (`END_POINT` / `OSS_BUCKET` 等) 共享 | coe lane 真写 prod bucket（可能加 lane prefix path 缓解，但不在 Phase 3） |
| 火山引擎 TOS (@volcengine/tos-sdk) | prod env 共享 | 同上 |
| Langfuse trace | prod `LANGFUSE_HOST` 共享，业务代码无 enabled guard | coe lane trace 写 prod Langfuse，按 lane tag 过滤（违反 memory rule `feedback_langfuse_trace_mandatory` 中 "必须接 trace" 的精神实际上保留了 trace，但 trace 数据混在 prod 观测里） |
| OpenAI / Azure / Google API | DB `ModelProvider` 表共享（不走 env/Bundle） | coe lane 调真 API 真花钱；接受 |
| 飞书 / pixiv / bangumi 等外部 API | prod 配置共享 | coe lane 真打外部，接受 |

**理由**：

- OSS / Langfuse / AI API 是第三方托管 SaaS（或 self-host 较重的 Langfuse），独立测试实例成本远高于"接受 coe lane 共享 prod"带来的污染
- AI Provider 配置在 DB ModelProvider 表，切 mock 不能简单 ClassOverrides，需要业务代码加 env override 或 DB seed，属另一个 phase 工程
- 这些都不会污染 chiwei-platform 内部状态化基建（prod PG / Redis / MQ / Qdrant / Mongo）—— Phase 3 完成后 prod 这五件套已被守住

**后续 phase 可能解决**：

- Phase 4+：OSS bucket 加 lane prefix path 避免覆盖 prod 文件（小改动，可拼到其他 phase 一起）
- Phase 5+：mock-feishu / mock-llm 服务（需 paas-engine 多 namespace 支持）—— 长期方向，当前不阻塞 coe lane 真跑业务

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| chiwei-test-mongo 没有 `chiwei` db / auth source 不对 | mongodb 镜像默认 root user 在 admin db；业务首次 insertEvent 会自动建 `chiwei` db。docker-compose 健康检查后跑一次 `mongosh --eval 'db.getSiblingDB("chiwei").stats()'` 提前验证 |
| qdrant `init_collections` 异步建表时序问题 | 沿用 Phase 1+2 已验证的 lifespan 启动顺序 |
| recall-worker 误加 bundle 引用 | 已在 §3.3 explicit 排除，plan task review 确认 |
| prod 行为意外变化 | bundle baseline 必须严格等于 prod 现值；plan 阶段提供 prod diff 对比工具 |

**回滚**：

- 单 bundle 回滚：`DELETE /api/paas/config-bundles/qdrant` + 从 4 App `config_bundles` 移除引用 → resolved-config 回到改动前
- 容器回滚：`docker-compose stop chiwei-test-qdrant chiwei-test-mongo` → 不影响 prod
- docker-compose.yaml git 回滚：revert commit
- paas-engine 自身不改动，无需回滚

## 8. 验证之后

- 把"已验证 coe lane 跑通 Qdrant + Mongo"+ "prod 零变化"两条证据贴回 [[project_dev_workflow_v2]] memory
- 更新 `project_phase3_inventory.md` 标记类 A 完成
- Phase 4 启动条件：contract test runner + ship 门禁 + image_digest 绑定（Phase 4 spec 另起）
