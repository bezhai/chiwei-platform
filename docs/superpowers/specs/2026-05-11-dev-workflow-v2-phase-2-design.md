# Dev Workflow v2 Phase 2 — Bundle ClassOverrides + 业务自动建表

承接 Phase 1（lane 命名前缀校验 + chiwei-test 基建容器）。本 phase 做的事：让 coe-* lane 部署时业务 pod 自动连到 chiwei-test 基建、自动有可用的 schema、不会因 operator 漏配静默打 prod。

## 为什么要做

Phase 1 结束时基础设施已经备好（chiwei-test-postgres / -rabbitmq / -redis 三个 docker 容器跑在 cpu1，chiwei-test K8s namespace + quota + netpol 就位），lane 命名前缀也强制校验了，但**没有任何机制把业务 pod 跟这些测试基建连起来**。现在如果起一个 coe-* lane 的 agent-service，它读到的 `POSTGRES_HOST` 还是 `postgres`（prod 实例），第一行 query 就直接打 prod 库。

paas-engine 现状：

- 业务 App 的基础设施 env（PG / Redis / RabbitMQ 那一坨）已经收敛到 ConfigBundle 了——`agent-service` 引用 `pg-main` `redis` `rabbitmq` 等 9 个 bundle，`lark-server` 引用 7 个，`lark-proxy` 引用 2 个。App.envs 和 Release.envs 完全没碰这些 key，single source。
- ConfigBundle 已经支持 `LaneOverrides map[lane_name]map[key]string`——可以按"lane 名字"覆盖单个 key。
- App 的 `ConfigBundles []string` 是**全 lane 共享**的——同一 App 在所有 lane 引用同一组 bundle。

差距在哪：

1. **per-lane override 是 fail-open 的**：每开一个新 coe lane（coe-alice / coe-bob / ...）都要去 `pg-main` `redis` `rabbitmq` 三个 bundle 各加一条 lane override。operator 漏配一次 → coe lane 静默连 prod 写脏数据。这跟 Phase 1 fail-closed 红线直接冲突。
2. **测试库表结构不存在**：chiwei-test-postgres 是空库。agent-service 17 张 SQLAlchemy 业务表 + lark-server 13 张 TypeORM 表 + lark-proxy 1 张 lane_routing 表，prod 现状是靠 `ops-db submit` 手写 DDL 维护——测试库不会自动有这些表，业务 pod 起来第一行 query 就 `relation does not exist` crash。
3. **lark-proxy 是入口网关**，逻辑上它必须连 prod 的 lane_routing 表才能做 lane 路由，本身就不该被部署到 coe-* lane（部署到 coe lane 没意义，反而会因为连不上 lane_routing 而 crash）。

## 目标

让 `make deploy APP=agent-service LANE=coe-foo` 这一条命令直接 work：业务 pod 自动连 chiwei-test 基建、表结构自动就位、operator 零手工配置、operator 想"漏配"也漏不掉。

不解决：

- 业务数据 fixture 注入（coe-* lane 业务表是空的，谁来塞测试数据）—— 留 Phase 3 mock 服务设计时配套
- 跨 coe-* lane 互相隔离（多人同时开 coe lane 会互相污染同一个测试库）—— Phase 5 做 schema/vhost/prefix 隔离
- ORM 大迁移（agent-service 17 张 SQLAlchemy → pydantic Data）—— 长期 backlog 已有
- framework 契约测试运行器 + image digest 绑定 ship 门禁 —— Phase 4

## 方案

### ConfigBundle 加 ClassOverrides 字段

ConfigBundle 现状：

```go
type ConfigBundle struct {
    Name          string
    Keys          map[string]string                       // baseline，所有 lane 共享
    LaneOverrides map[string]map[string]string            // lane name → key → value
}
```

加一个新字段：

```go
type ConfigBundle struct {
    Name           string
    Keys           map[string]string
    ClassOverrides map[string]map[string]string           // lane class → key → value，新增
    LaneOverrides  map[string]map[string]string
}
```

`ClassOverrides` 的 key 是 lane class 字符串（"coe" / "ppe"，prod 不需要因为 baseline 就是 prod）。**配一次**所有该类的 lane 自动套用。比如：

```yaml
# pg-main bundle:
keys:
  POSTGRES_HOST: postgres
  POSTGRES_PORT: "5432"
  POSTGRES_USER: chiwei
  POSTGRES_PASSWORD: <prod_password>
  POSTGRES_DB: chiwei
class_overrides:
  coe:
    POSTGRES_HOST: <chiwei-test-postgres-host>
    POSTGRES_PORT: "5433"
    POSTGRES_USER: chiwei
    POSTGRES_PASSWORD: <test_password>
    POSTGRES_DB: chiwei_test
```

所有 coe-* lane 自动拿到下半截值，无需每条 lane 手工配。redis / rabbitmq bundle 同理。

### Resolve 函数新优先级

paas-engine 现有两个 resolve 函数都要改：

- `ResolveBundleEnvs(app, lane) → envs`：deployer 部署时用，只看 bundle 层
- `ResolveConfig(app, lane) → envs[]`：管理 API 用（GET resolved-config），看完整层级

现状 bundle 层优先级（低 → 高）：`baseline → lane override`

新 bundle 层优先级：`baseline → class override → lane override`

class override 在 baseline 之上、lane override 之下——保留 per-lane override 给 debugging 时单独 patch 一个 lane 的能力（比如 coe-foo 临时换个 PG 端口测兼容性）。

完整 ResolveConfig 优先级（低 → 高）变成：`bundle baseline → bundle class override → bundle lane override → app.Envs → release.Envs → auto-injected`

prod / blue 这类保留名走 `LaneClassProd`，class override map 里没有 "prod" key（baseline 就是 prod 值），所以保留名 lane 拿到的还是 baseline，零变化。

### Fail-closed 部署校验

光加 ClassOverrides 不够，operator 仍然可能忘了配 coe class override 就 deploy。fail-closed 校验要求：

**coe-* lane 部署时，paas-engine 必须校验关键 bundle 都有 coe class override。** 没有就 reject 部署。

实现方式：在 ConfigBundle 上加一个 `RequiredKeys map[string][]string` 字段，标记每个 lane class 必须**完整覆盖**哪些 key：

```go
type ConfigBundle struct {
    Name           string
    Keys           map[string]string
    ClassOverrides map[string]map[string]string
    LaneOverrides  map[string]map[string]string
    RequiredKeys   map[string][]string                    // 新增，class → 必须 override 的 key list
}
```

例：

```yaml
# pg-main bundle:
required_keys:
  coe: [POSTGRES_HOST, POSTGRES_PORT, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB]
```

校验逻辑：

- 校验入口：`ReleaseService.CreateOrUpdateRelease`，跟 Phase 1 的 `ClassifyLane` 校验串行
- 校验内容：根据 lane 的 class，遍历 `app.ConfigBundles` 引用的每个 bundle，对该 bundle 的 `RequiredKeys[class]` 列出的每个 key，必须在 `bundle.ClassOverrides[class]` 里有非空值
- 任一 key 缺失或空值即 reject，error message 明示**具体哪个 bundle 哪个 key 缺**

为什么校验 key list 而不是只校验 ClassOverrides[class] 非空：operator 可能只 override 了 `POSTGRES_HOST` 却漏了 `POSTGRES_PASSWORD/DB`，剩下的 key fallback 到 baseline = prod 值——结果是 coe-* lane 连了测试 host 的 prod database 用 prod password，比"完全连 prod"更危险（混合状态、错误更难复现）。RequiredKeys 强制完整覆盖。

这样：

- 三个基建 bundle 标记 `RequiredClasses = ["coe"]` 一次
- 第一个 coe-* lane deploy 之前 operator 必须先把 ClassOverrides[coe] 配上，否则被 reject
- 之后开新的 coe-foo / coe-bar 不需要任何额外配置
- 配错了（比如打字错把 ClassOverrides[coe] 写成 ClassOverrides[con]）也会被 fail-closed 校验抓到——空 = reject

值匹配（"如果 PG_HOST 解析出来跟 prod 一样就 reject"）方案放弃了——fragile（prod hostname 可能改、可能多个 prod 实例）、又把"哪些 key 是 prod 标识"散到校验逻辑里。显式 RequiredClasses 标注更直白。

### lark-proxy 部署门禁

lark-proxy 是飞书 webhook 入口，必须读 prod PG 的 lane_routing 表才能做路由——它**永远只在 prod lane 跑**。在 coe-* lane 部署 lark-proxy 没有意义（连不上 lane_routing → crash）。

paas-engine 部署时按"App 名 + lane class"硬白名单校验：lark-proxy 只允许部署到 `LaneClassProd`，部署到 coe-* / ppe-* 直接 reject。

实现方式：在 App domain 上加一个 `AllowedLaneClasses []string` 字段（默认 nil = 全允许），lark-proxy 设 `["prod"]`。校验逻辑放在 ReleaseService.CreateOrUpdateRelease，跟 Phase 1 的 ClassifyLane 校验一起跑。

### agent-service coe-* lane 自动建表

agent-service 17 张 SQLAlchemy 业务表（lark_user / lark_group_chat_info / conversation_messages / bot_persona / akao_schedule / life_engine_state / glimpse_state / memory_entity / reply_style_log / fragment / abstract_memory / memory_edge / notes / schedule_revision / model_provider / model_mappings / lark_base_chat_info / ...）prod 是 ops-db submit 手建的。coe-* lane 测试库要让它们自己建。

在 `apps/agent-service/app/main.py` lifespan 起手处加一段：

```python
if settings.lane and settings.lane.startswith("coe-"):
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:
        logger.exception("auto create_all failed for coe lane, aborting startup")
        raise
```

**严格只在 `coe-*` lane 触发**——白名单语义，且**仅限 coe-***：

- `prod` / `blue` 绝不自动建表（边界由 ops-db submit 维护）
- **`ppe-*` 也绝不自动建表**——ppe-* 连的是 prod 基建（Phase 1 spec 定的：ppe-* = pre-prod env、连 prod 基建做灰度/AB），ppe-* lane 跑 create_all 等于在 prod DB 上 create_all，是 catastrophic
- `LANE` env 没注入时（None）一律 fallback 到不建表
- 未来加新 lane class 必须显式更新这个判断

**create_all 失败 = pod 启动失败**：`raise` 抛出去让 lifespan 失败、pod CrashLoopBackoff、operator 立刻看到。绝不 swallow——否则 schema 漂移/权限不足/连错库会表现成业务请求随机 crash，排查成本高。

`create_all` 是幂等的、`IF NOT EXISTS` 语义，每次启动跑一遍不会破坏已有表。

`settings.lane` 来源是 K8s 自动注入的 `LANE` env（`apps/paas-engine/internal/adapter/kubernetes/deployer.go:270` 注入），`apps/agent-service/app/infra/config.py:118` 已经在读。

### 多 Deployment 同镜像 schema 启动顺序

**一镜像多 Deployment 是 chiwei-platform 的特点**（CLAUDE.md 镜像与服务映射表）：

- agent-service 镜像 → 2 个 Deployment：**agent-service**（HTTP）+ **vectorize-worker**
- lark-server 镜像 → 3 个 Deployment：**lark-server**（HTTP）+ **recall-worker** + **chat-response-worker**

worker 进程入口可能不走 HTTP 服务的 `main.py` lifespan——例如 vectorize-worker 通常 `python -m app.workers.vectorize` 直接起，不经过 FastAPI lifespan，自然不会触发 create_all。如果 worker 先于 HTTP 服务起，业务表还不存在，worker 第一个 query 就 crash。

**Phase 2 处理方案**：

1. **schema bootstrap 抽出独立函数** `ensure_business_schema()`，放在 `apps/agent-service/app/data/__init__.py` 或 `app/data/bootstrap.py`，包含上面的 coe-* 守门 + create_all + raise on failure
2. **HTTP 服务入口（main.py lifespan）调用它**
3. **每个 worker 入口启动时也调用它**（vectorize-worker 入口、agent-service 镜像里其他可能的 worker 入口）
4. lark-server 同理：把 SYNCHRONIZE_DB=true 的语义不只让 HTTP 服务跑——recall-worker / chat-response-worker 这两个 Deployment 也必须挂 `lark-server-runtime` ConfigBundle（这点要在 paas-engine App 配置里手动确保），TypeORM datasource initialize 时同样会 sync schema

**幂等 + 竞态**：create_all / SYNCHRONIZE 都是 IF NOT EXISTS 语义，多个 Deployment 同时启动 race 也无害（最坏多打几条 DDL，PG 内部串行化）。

**worker 入口清点**：实施 Phase 2 时必须 grep 一遍 agent-service / lark-server 镜像里所有 worker 入口（`apps/agent-service/app/workers/` 等目录 + Dockerfile CMD 变体 + paas-engine 里这些 App 的 Command 配置），逐个加 ensure_business_schema 调用。一个漏掉 → 该 worker 在 coe-* lane 起来必 crash，反过来又是 fail-fast 信号。

跟 dataflow runtime 自带的 `runtime_for_sources.migrate_schema()`（管 outbox / inflight / dlq_audit / glimpse_request 等 pydantic Data 类）共存——前者管 Data 类、本提案管 SQLAlchemy 业务表，互不重叠。

### lark-server SYNCHRONIZE_DB=true coe override

lark-server TypeORM 看 `SYNCHRONIZE_DB === 'true'` 决定是否自动 sync schema。prod 一直是 false（必须）。新建一个专属 ConfigBundle `lark-server-runtime`，只 lark-server 引用，内容：

```yaml
keys:
  SYNCHRONIZE_DB: "false"          # baseline 强制 false 防漏给 prod
class_overrides:
  coe:
    SYNCHRONIZE_DB: "true"
```

把 `lark-server-runtime` 加到 lark-server App 的 `ConfigBundles` 列表。prod / blue / ppe-* 拿到 baseline `false`，coe-* 拿到 `true`。

把 SYNCHRONIZE_DB 隔离到一个专属 bundle、不蹭已有 bundle，是为了"这是 coe 专属的危险开关"在审计层面一目了然——任何人翻 ConfigBundle 列表就能看到这个 bundle 的存在和它的覆盖语义。

### 跨 service 重叠表的 schema 漂移

agent-service (SQLAlchemy) 和 lark-server (TypeORM) 都定义了 `LarkUser` / `ConversationMessage` 等同名表，字段不一定逐字段一致。create_all + SYNCHRONIZE 都是 IF NOT EXISTS 语义——谁先起就建谁那一版，后起的拿着自己 ORM 的 schema 跟实际表对不上时会运行时 query 错（缺列 / 类型不匹配）。

**这是 Phase 2 已知的、不解决的 risk**：

- prod 不存在这个问题——schema 是 ops-db submit 手建的独立 source-of-truth，两边 ORM 都跟它对齐
- 测试环境暴露这个漂移**反而是测试价值**——能在 ship 前发现"两边 ORM 对不上"，比 prod 出 bug 强
- 真实暴露之后处理路径：要么修两边 ORM 让它们字段一致，要么在 backlog 里推动 ORM 大迁移（17 张 SQLAlchemy → pydantic Data）

spec 不引入"哪边 ORM 是 truth"机制——这会引入虚假兼容感，反而掩盖漂移。

### Rollout 顺序（避免中间态把所有 coe 部署硬拒）

paas-engine 加 `RequiredKeys` 校验和先有 ClassOverrides 之间有顺序约束——如果 paas-engine 先升级且开了校验，但 ClassOverrides 还没配进去，所有正在跑的 coe-* lane 部署会立刻被拒。

**Rollout 必须按这个顺序**（每步必须验证完成才进下一步）：

1. paas-engine 升级到带 ClassOverrides + RequiredKeys 字段的版本，但**`RequiredKeys` 字段先全部留空**（校验不触发）
2. operator 通过 paas API 给 `pg-main` `redis` `rabbitmq` `lark-server-runtime` 写入 `ClassOverrides[coe]`，验证 `GET /api/paas/apps/agent-service/resolved-config?lane=coe-validation` 解析出的 PG/Redis/MQ 值确实指向 chiwei-test 基建
3. 给这 4 个 bundle 加 `RequiredKeys[coe]`，校验生效
4. 业务代码（agent-service create_all + lark-server-runtime bundle 引用 + worker 入口 ensure_business_schema）部署
5. 端到端验证：`make deploy APP=agent-service LANE=coe-validation` + `make deploy APP=lark-server LANE=coe-validation`

第 1-3 步在 paas-engine 自己的部署窗口内做，第 4-5 步是业务部署，分两个窗口。

### 运维 runbook：prod schema 变更后同步测试库

prod schema 通过 ops-db submit 加列后，下一次 coe-* lane 启动时 SQLAlchemy `create_all` 会发现表已存在不再 sync，新列拿不到——业务 query 新列时报错。

**Phase 2 阶段的处理**（Phase 5 真正隔离 schema 之前的临时机制）：每次 prod schema 变更（ops-db submit DDL 后）operator 必须手工 drop chiwei-test-postgres 里对应的表，让下一次 coe lane 启动重建：

```sql
-- 在 chiwei-test-postgres 执行（不是 prod！）
DROP TABLE IF EXISTS <被改的表名> CASCADE;
```

这个步骤要写进 ops-db submit 的 PR template / 合码检查清单（CLAUDE.md "上线前必须完成的检查"段落顺带提一下）。Phase 5 上 schema-per-lane 隔离后取消这个手工步骤——届时 coe lane undeploy 自动清理 schema、新 lane 部署自动建。

### 测试基建 ConfigBundle 内容

需要给 paas-engine 注入下列 ClassOverrides：

| Bundle | ClassOverrides[coe] keys |
|---|---|
| pg-main | POSTGRES_HOST / PORT / USER / PASSWORD / DB → 指向 chiwei-test-postgres |
| redis | REDIS_HOST / PORT / PASSWORD → 指向 chiwei-test-redis |
| rabbitmq | RABBITMQ_URL → 指向 chiwei-test-rabbitmq |
| lark-server-runtime（新建） | SYNCHRONIZE_DB=true |

具体连接串值在 cpu1 `~/.chiwei-test-env.env`，部署期通过 paas API 把 ClassOverrides 设进去（不入 git）。

## 不在范围

| 项 | 推到 |
|---|---|
| 业务数据 fixture（coe-* lane 业务表是空的） | Phase 3（mock 服务一起设计） |
| mock 飞书 / mock 外部 API services | Phase 3 |
| framework 契约测试运行器 + ship 门禁 + image digest 绑定 | Phase 4 |
| 跨 coe-* lane 内部隔离（schema / vhost / prefix） | Phase 5 |
| coe-* lane undeploy 时清理 schema/vhost/prefix | Phase 5 |
| 资源熔断 baseline（statement_timeout / queue TTL / key TTL） | Phase 5 |
| Mock 契约漂移检测 | Phase 6 |
| ORM 大迁移（17 张 SQLAlchemy → pydantic Data） | 长期 backlog |
| dynamic config SDK 切换基础设施连接串路径 | 不做（违背 CLAUDE.md "基础设施走 ConfigBundle"原则，本 spec 选 ConfigBundle 路径） |

## 已知 risk

1. **跨 service 重叠表 schema 漂移**：见上"跨 service 重叠表的 schema 漂移"——故意不解决，让测试环境暴露问题。
2. **多人同时开 coe-* lane 互相污染**：所有 coe-* lane 共享同一个 chiwei-test-postgres / -rabbitmq / -redis 实例，互相会读到对方的脏数据。Phase 5 才隔离。Phase 2 阶段的解决方式：约定一次只一个 coe lane（人不多）。
3. **测试库 schema 跟 prod 加列漂移**：处理见上"运维 runbook"段——每次 prod ops-db submit 加列后必须手工 drop chiwei-test-postgres 对应表。Phase 5 schema-per-lane 隔离后取消。
4. **lark-server SYNCHRONIZE_DB=true 在测试库可能 drop 列**：TypeORM synchronize 的语义是"sync 到 entity 定义"，如果 entity 删了列，DB 也会删——测试库可接受，但 spec 要写明"绝不能让 SYNCHRONIZE_DB=true 漏到 prod / blue / ppe-*"，所以 ClassOverrides[coe] 是唯一的赋值点。

## 设计决策记录

### 为什么不走 dynamic config SDK 路径

最初讨论时提过让业务 SDK 调 `dynamic_config.get("POSTGRES_HOST")` 运行时拉。否决理由：

1. **违背 CLAUDE.md 项目原则**：CLAUDE.md 明写"基础设施连接（DB/Redis）走 ConfigBundle（部署时环境变量）"，业务行为参数才走 dynamic config。
2. **改动散布**：agent-service / lark-server / lark-proxy 每个连接初始化点都要改（agent-service 至少 3 处：session.py / redis.py / rabbitmq.py），改动量远大于改一个 paas-engine。
3. **连接池语义被破坏**：连接串运行时变化时，已建立的连接池不知道要切——会出现"配置改了但旧连接还在"的灰区，调试困难。
4. **没有运行时切 lane 的真实场景**：coe-* lane 是独立部署，pod 整个生命周期 lane 不变，env 一次性派完全够用。

### 为什么不 dump prod schema 到测试库

最初讨论过 `pg_dump --schema-only` prod → 灌进 chiwei-test-postgres 的方案。bezhai 选 B（每个 ORM 各管各的 schema）。理由：

- 测试库跟 prod **完全隔离**——schema 来源也独立，能在测试环境主动暴露两边 ORM 跟 prod schema 的漂移（漂移是真问题，掩盖它没价值）。
- dump 流程引入额外维护负担——每次 prod schema 变更都要手工 dump 重灌，容易忘。
- ORM 各自 sync 是测试环境长期更可持续的方案——测试环境本来就是跑 ORM 当前状态，跟"测试代码当前定义"对齐。

### 为什么 RequiredClasses 不用值匹配

最初考虑过"如果 coe lane 解析出来的 PG_HOST 跟 prod 值一样就 reject"。否决理由：

1. **fragile**：prod hostname 会改、prod 可能有多个实例（比如读副本）、不同环境 prod 值不同——硬编码 prod 值匹配维护噩梦。
2. **责任分散**：每个校验点都要知道"哪些 key 算 prod 标识"，这种知识在多个 bundle / 多种资源类型上重复。
3. **绕得开**：值不一样但仍指向 prod 实例就绕过（比如同一 PG 实例的另一个 hostname）。

显式 `RequiredClasses` 标注由 bundle 自己声明"我必须有这个 class 的 override"，校验逻辑统一、责任收敛、绕不开。

## 验收标准

- [ ] paas-engine ConfigBundle struct 加 ClassOverrides + RequiredClasses 字段，DB schema migration 落地
- [ ] ResolveBundleEnvs 优先级测试覆盖：baseline / class override / lane override / app envs / release envs 五层 merge 顺序
- [ ] coe-* lane 部署时 RequiredClasses 校验生效——`pg-main` 没配 ClassOverrides[coe] 时 deploy 必 reject，error message 明示
- [ ] App.AllowedLaneClasses 校验生效——lark-proxy 部署到 coe-* / ppe-* 必 reject
- [ ] agent-service `make deploy APP=agent-service LANE=coe-validation` 端到端：HTTP pod + vectorize-worker pod 都起来连 chiwei-test-postgres、都触发 ensure_business_schema、create_all 17 张表、第一个业务请求不 crash
- [ ] lark-server `make deploy APP=lark-server LANE=coe-validation` 端到端：lark-server + recall-worker + chat-response-worker 三个 Deployment 都起来、都拿到 SYNCHRONIZE_DB=true、TypeORM 自动 sync 13 张表
- [ ] ppe-* lane（举一个 ppe-validation 例子）部署：业务 pod 不触发 create_all、不 sync schema（守门只 coe-* 生效的反向验证）
- [ ] RequiredKeys 校验反向测试：故意把 `pg-main.ClassOverrides[coe]` 删一个 key 后再 deploy coe lane，必须被 reject、error 明示哪个 key 缺
- [ ] 验证完毕清理 coe-validation / ppe-validation lane

## 不引入的复杂度

- 不引入"测试库自动从 prod 同步 schema 变更"机制
- 不引入"per-lane bundle 引用切换"（让 App 在 coe-* 引用 pg-test 在 prod 引用 pg-main）—— ClassOverrides 已经解决问题，per-lane bundle 引用是更复杂的同义重复
- 不引入 dynamic config SDK 客户端的基础设施连接串模式
- 不引入跨 service ORM schema 协调机制（让漂移自然暴露）

## 附录：codex T1 评审历程

第一轮 review 出 3 必改 + 3 建议，**全采纳**：

- **必改 1**：`RequiredClasses` 只校验"非空"不够——operator 漏配单个 key（比如只 override POSTGRES_HOST 漏 PASSWORD/DB）会让 coe-* lane 拿混合 prod 值，比"完全连 prod"更隐蔽。改成 `RequiredKeys map[class][]string` 强制完整 key 集合校验。
- **必改 2**：原 spec 让 agent-service 在 `coe-*` 和 `ppe-*` 都跑 create_all，但 ppe-* 连 prod 基建——等于在 prod DB 上跑 create_all。create_all 守门收紧到只 `coe-*`。
- **必改 3**：一镜像多 Deployment 没覆盖——agent-service 镜像还有 vectorize-worker、lark-server 镜像还有 recall-worker / chat-response-worker，worker 入口可能不走 main.py lifespan。新增"多 Deployment 同镜像 schema 启动顺序"章节，schema bootstrap 抽成独立 `ensure_business_schema()` 函数 + 每个 worker 入口手工调用。
- **建议 1**：rollout 顺序——paas-engine 升级 + RequiredKeys 校验 + ClassOverrides 配置之间有顺序约束，先 deploy 后配置会把所有 coe lane 硬拒。新增"Rollout 顺序"段。
- **建议 2**：create_all 失败要明确 = pod 启动失败（CrashLoopBackoff），不能 swallow 让后续业务请求随机 crash。spec 加 `try/except + logger.exception + raise`。
- **建议 3**：prod schema 加列后手工 drop 测试库表是人工步骤，必须写成显式 runbook。新增"运维 runbook"段，并入 ops-db submit PR template。

最终 spec 现状定型。
