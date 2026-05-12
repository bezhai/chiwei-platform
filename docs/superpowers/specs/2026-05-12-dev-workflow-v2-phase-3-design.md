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

- 类 A 内部基建外延：**扩展现有 `qdrant` 和 `mongo` bundle**（prod 早已存在，但 Phase 2 没给加 class_overrides/required_keys）+ ClassOverrides[coe] + RequiredKeys[coe]，让 coe-* lane 派 chiwei-test 容器 endpoint。**App config_bundles 引用无需追加**——所有 5 个 App (agent-service / vectorize-worker / lark-server / chat-response-worker / recall-worker) 早已引用 mongo / qdrant bundle，本 Phase 只在 bundle 层做改动。
- 新建 cpu1 docker 容器：chiwei-test-qdrant + chiwei-test-mongo
- Phase 2 漏 commit 修复：`infra/test-env/docker-compose.yaml` 的 rabbitmq image 改动同步进 git（image 切到 fixed tag/digest，非 `:latest`）

**Out of scope**（known limitation，§6 详述）：

- 外部 SaaS 不隔离：OSS（阿里云 ali-oss + 火山 TOS）、Langfuse、AI Provider API（OpenAI/Azure/Google）
- mock 服务（mock-feishu / mock-llm 等）
- paas-engine 多 namespace 部署支持
- lite-registry 多 namespace watch

## 3. 设计

### 3.1 测试容器（cpu1 docker-compose 追加）

`infra/test-env/docker-compose.yaml` 新增两个 service，image tag 固定（不用 `:latest` 避免漂移）：

**chiwei-test-qdrant**:
- image: `qdrant/qdrant:v1.11.0`（或更具体的 sha256 digest）
- ports: `16333:6333`（HTTP REST API）/ `16334:6334`（gRPC）
- env: `QDRANT__SERVICE__API_KEY=${CHIWEI_TEST_QDRANT_API_KEY:?...}`
- volumes: 持久化 storage（参考 chiwei-test-postgres 数据目录约定）

**chiwei-test-mongo**:
- image: `mongo:7.0.14-jammy`（或更具体 digest）
- ports: `27018:27017`
- env: `MONGO_INITDB_ROOT_USERNAME=chiwei-test` / `MONGO_INITDB_ROOT_PASSWORD=${CHIWEI_TEST_MONGO_PASSWORD:?...}`
- volumes: 持久化 data 目录

**网络边界**（沿用 Phase 1 chiwei-test-postgres / -rabbitmq 同款 host port mapping `"host_port:container_port"`）：

`docker-compose` ports 不带 bind IP（等价 `0.0.0.0:port`，宿主机所有网卡监听）。**安全性靠的是网络层、不是 docker bind**：

- cpu1 节点（10.37.6.235）只有内网 IP，**无公网 IP**，外网无法直接摸到
- k3s prod ns 通过 cpu1 内网 IP 访问 chiwei-test-* 容器
- 同机 docker container 通过 host network 互通

**弱密码可接受**的前提是上述网络边界成立。Phase 3 不改 ports 形态（与 Phase 1+2 模板一致），但 spec explicit 声明此边界依赖。后续如要再加防御层（如 `127.0.0.1:port:port` 限本机 + 走 SSH 隧道暴露），属另一个 phase。

### 3.2 ConfigBundle 设计

**扩展现有 `qdrant` 和 `mongo` bundle**（prod 早已存在，但 class_overrides / required_keys 均为 NULL）。

**`qdrant` bundle**（prod 现 baseline 含 4 个 key，包括 1 个仓库无引用的死 key `QDRANT_API_KEY`——可能是 sidecar/外部组件遗留，为了 byte-equal + RequiredKeys 完整覆盖原则全部 ClassOverrides 都要派）：

| 字段 | 值 |
|---|---|
| keys (baseline，保持 prod 现值不动) | `QDRANT_SERVICE_HOST=qdrant` / `QDRANT_SERVICE_PORT=6333` / `QDRANT_SERVICE_API_KEY=<prod 现值>` / `QDRANT_API_KEY=<prod 现值，死 key>` |
| `class_overrides[coe]` | `QDRANT_SERVICE_HOST=10.37.6.235` / `QDRANT_SERVICE_PORT=16333` / `QDRANT_SERVICE_API_KEY=<chiwei-test 弱密钥>` / `QDRANT_API_KEY=<chiwei-test 弱密钥，跟前一个相同也行>` |
| `required_keys[coe]` | `["QDRANT_SERVICE_HOST", "QDRANT_SERVICE_PORT", "QDRANT_SERVICE_API_KEY", "QDRANT_API_KEY"]`（完整覆盖原则） |

**`mongo` bundle**（prod 现 baseline）：

| 字段 | 值 |
|---|---|
| keys (baseline，保持 prod 现值不动) | `MONGO_HOST=mongodb` / `MONGO_INITDB_ROOT_USERNAME=chiwei` / `MONGO_INITDB_ROOT_PASSWORD=<prod 现值>` |
| `class_overrides[coe]` | `MONGO_HOST=10.37.6.235:27018` / `MONGO_INITDB_ROOT_USERNAME=chiwei-test` / `MONGO_INITDB_ROOT_PASSWORD=<chiwei-test 弱密码>` |
| `required_keys[coe]` | `["MONGO_HOST", "MONGO_INITDB_ROOT_USERNAME", "MONGO_INITDB_ROOT_PASSWORD"]` |

**MONGO_HOST 含 port 仅适用于当前 client 实现**：本 Phase 范围内的 mongo client 共 3 处：

- `apps/lark-server/src/infrastructure/dal/mongo/client.ts:23-27` — 直拼 `mongodb://${user}:${pwd}@${host}/chiwei?authSource=admin`，`${host}` 不读 MONGO_PORT
- `apps/monitor-dashboard/src/mongo.ts:31-36` — 同款直拼
- `packages/ts-shared/src/mongo/types.ts:30-31` — **拼 `host` + `:${port}` 分两 env 读**，如果 MONGO_HOST 含 port 会变成 `host:port:27017` 烂掉

**ts-shared/mongo 当前全仓 0 个 import**（`grep -rn "ts-shared/mongo" apps/` 返回空），是死代码。如果未来有 App 通过 ts-shared 用 mongo，**必须 split 成 MONGO_HOST + MONGO_PORT 两个 bundle key**，并改 `lane_overrides[coe]` 派 PORT。本 Phase 不解决此潜在风险，但需在 spec 显式记录。

同时注意 lark-server/monitor-dashboard 写死 db 名 `chiwei` + `authSource=admin`，chiwei-test-mongo 容器 root user 必须在 admin db（mongo 镜像默认行为）+ 业务首次写入自动建 `chiwei` db。

### 3.3 App `config_bundles` 引用——**无需改动**

prod 现状（来自 `paas_engine.apps.config_bundles` 表）：

| Deployment | 镜像 | 现引用 bundles |
|---|---|---|
| agent-service | agent-service | pg-main, redis, **qdrant**, langfuse, forward-proxy, ai-provider, search-apis, inter-service-auth, rabbitmq |
| vectorize-worker | agent-service | pg-main, redis, rabbitmq, **qdrant**, langfuse, ai-provider, inter-service-auth |
| lark-server | lark-server | pg-main, redis, **mongo**, oss, inter-service-auth, rabbitmq, ai-provider, lark-server-runtime |
| chat-response-worker | lark-server | pg-main, redis, **mongo**, oss, inter-service-auth, rabbitmq, ai-provider, lark-server-runtime |
| recall-worker | lark-server | pg-main, redis, **mongo**, oss, inter-service-auth, rabbitmq, ai-provider, lark-server-runtime |

**所有 5 App 早已引用 mongo / qdrant bundle**，本 Phase 不动 App config_bundles。

**冗余说明**：`recall-worker` 业务代码不连 mongo / qdrant（盘点：仅 PG + 飞书 API），但引用了 mongo bundle 并通过 RequiredKeys[coe] 校验。这是 Phase 2 留下的 over-reference，无功能影响（启动时 mongo env 被注入但不用）。本 Phase 不清理此冗余以避免范围漂移。

### 3.4 Phase 2 漏 commit 修复

Phase 2 实际把 cpu1 chiwei-test-rabbitmq 切到 `harbor.local:30002/inner-bot/rabbitmq`（含 `rabbitmq_delayed_message_exchange` plugin），但 `infra/test-env/docker-compose.yaml` 仓库版本未 commit。

Phase 3 PR 同步把这个改动 commit 进 git，**且 image tag 必须固定**（不能用 `:latest`，否则会把 Phase 2 的"未 commit 漂移"转化为新的"不可复现漂移"）。具体 tag 选择：plan 阶段 `docker images | grep rabbitmq` 拿到当前在跑容器的实际 image digest 或 specific tag 写进 docker-compose.yaml。

PR 文案在 commit message 单独标注"Phase 2 missed commit fix"，便于追溯。

部署前在 plan 阶段 `docker exec` 验证当前容器的 `rabbitmq_delayed_message_exchange` plugin 已 enabled（避免 image 换 tag 后 plugin 丢失）。

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

**注意**：chiwei-test-qdrant / -mongo 启用持久化 volume，**单纯"collection 存在 / count > 0" 可能是上次 deploy 残留**——验收必须用 before/after diff 或 deploy 时间窗口 marker。

| 验收项 | 验证方式 | 期望 |
|---|---|---|
| bundle 更新成功 | `GET /api/paas/config-bundles/qdrant` + `mongo` | 200 + 含 class_overrides[coe] + required_keys[coe] |
| ClassOverrides 真生效 | `GET /api/paas/apps/agent-service/resolved-config?lane=coe-validation` | `QDRANT_SERVICE_HOST=10.37.6.235`，source `qdrant[class:coe]` |
| RequiredKeys 反向 reject | 故意删 bundle class_overrides[coe] 某 key 后 deploy coe lane | HTTP 400 |
| `init_collections` 真打测试 Qdrant | deploy 前先 `curl http://10.37.6.235:16333/collections \| jq` 记录 baseline；deploy agent-service 到 coe-validation；deploy 后再 list；diff | deploy 后 collections 含 4 个 chiwei collection（messages_recall / messages_cluster / memory_fragment / memory_abstract）。如果 baseline 已存在则比对 `created_at` 字段或 `vectors_count=0`；不能仅靠"存在"判 pass |
| `insertEvent` 真写测试 Mongo | 记录 deploy 时间 `T0`；deploy lark-server 到 coe-validation；发飞书测试消息；`ssh cpu1 docker exec chiwei-test-mongo mongosh --quiet --eval 'JSON.stringify(db.lark_event.findOne({}, {sort:{_id:-1}}))'` | 最新一条 event 的 `_id`（ObjectId 含时间戳）≥ T0；count 必须 ≥ 1 |
| prod 零变化 | `GET /api/paas/apps/<app>/resolved-config?lane=prod` 改动前后 byte-equal | 改 bundle keys baseline 必须等于 prod 现值；resolved-config diff 应为空 |

## 6. Known limitation（不在 Phase 3 范围）

**外部 SaaS / 第三方服务在 coe-* lane 仍指 prod**，本 Phase 不解决，但**需有缓解措施约束 coe lane 测试不破坏 prod 共享状态**：

### 6.1 各项服务现状 + coe lane 行为

| 服务 | 现状 | coe lane 行为 |
|---|---|---|
| 阿里云 OSS (ali-oss SDK) | prod env (`END_POINT` / `OSS_BUCKET` 等) 共享 | coe lane 真写 prod bucket（**风险：可能覆盖 prod 关键文件**，见 §6.2 缓解） |
| 火山引擎 TOS (@volcengine/tos-sdk) | prod env 共享 | 同上 |
| Langfuse trace | prod `LANGFUSE_HOST` 共享，业务代码无 enabled guard | coe lane trace 写 prod Langfuse，按 lane tag 过滤（trace 数据混在 prod 观测里，accepted） |
| OpenAI / Azure / Google API | DB `ModelProvider` 表共享（不走 env/Bundle） | coe lane 调真 API 真花钱；accepted |
| 飞书 / pixiv / bangumi 等外部 API | prod 配置共享 | coe lane 真打外部，accepted |

### 6.2 OSS / TOS 共享 prod 的缓解措施（mandatory，Phase 3 必须遵守）

OSS / TOS 是**有状态可变共享存储**，最危险——coe lane 测试可能 (a) 覆盖 prod 关键文件 (b) 删除 prod 文件 (c) 上传大量测试数据撑爆 bucket。**Phase 3 验证流程必须**：

1. **测试输入限定**：coe lane 端到端只发"接收型"飞书消息（纯文本 / 接收图片）。**禁止触发任何 OSS / TOS 写入路径**——即在验证前明确"agent-service 不会主动上传图片"、"lark-server 不会触发 Pixiv 重新下载到 OSS"等。
2. **PR 实施前 grep 确认**：plan 阶段必须 grep lark-server `client.put` / `tos.uploadFile` 等写路径，列出**所有**会触发 OSS/TOS 写的代码路径，确认验证场景**不会**走到这些路径。
3. **写入路径触发时立即停止**：如果某个 OSS 写入意外触发，立即 undeploy coe lane 服务，告警 bezhai。
4. **本 Phase 不实施 lane-prefix path 改造**（如 `chiwei-test/coe-foo/...`），但作为后续 phase（建议 Phase 4）的待办列入 §8。

### 6.3 后续 phase 可能解决

- Phase 4+：OSS / TOS bucket key 加 lane prefix path 避免覆盖 prod 文件（业务代码改动）
- Phase 5+：mock-feishu / mock-llm 服务（需 paas-engine 多 namespace 支持）
- 这些都不阻塞 Phase 3 完成 —— Phase 3 完成后 prod 五件内部基建（PG / Redis / MQ / Qdrant / Mongo）已守住

## 7. 风险与回滚

| 风险 | 缓解 |
|---|---|
| chiwei-test-mongo 没有 `chiwei` db / auth source 不对 | mongodb 镜像默认 root user 在 admin db；业务首次 insertEvent 会自动建 `chiwei` db。docker-compose 健康检查后跑一次 `mongosh --eval 'db.getSiblingDB("chiwei").stats()'` 提前验证 |
| qdrant `init_collections` 异步建表时序问题 | 沿用 Phase 1+2 已验证的 lifespan 启动顺序 |
| recall-worker 误加 bundle 引用 | 已在 §3.3 explicit 排除，plan task review 确认 |
| prod 行为意外变化 | bundle baseline 必须严格等于 prod 现值；plan 阶段提供 prod diff 对比工具 |

**回滚**：

- 单 bundle 回滚：`PUT /api/paas/config-bundles/qdrant` 把 `class_overrides` 和 `required_keys` 改回 `null`（baseline keys 不动）→ resolved-config 回到改动前；mongo bundle 同款
- 容器回滚：`docker-compose stop chiwei-test-qdrant chiwei-test-mongo` → 不影响 prod
- docker-compose.yaml git 回滚：revert commit（包括 rabbitmq image tag 改动）
- paas-engine 自身代码无改动，无需回滚

## 8. 验证之后

- 把"已验证 coe lane 跑通 Qdrant + Mongo"+ "prod 零变化"两条证据贴回 [[project_dev_workflow_v2]] memory
- 更新 `project_phase3_inventory.md` 标记类 A 完成
- Phase 4 启动条件：contract test runner + ship 门禁 + image_digest 绑定（Phase 4 spec 另起）
