# Dev Workflow v2 — 测试环境隔离

## 为什么要做

dataflow runtime 一上线就一堆 framework 类 bug：v5 踩过 dict→JSONB encoder 漏、created_at 撞 reserved 列；7a 踩过 aio_pika 双 ack、main.py vs Runtime.run() 双入口；7b 踩过 migrator 不 quote 标识符撞保留字、admin endpoint 不通过 PAAS_API。共同点是单测全过、上线必炸——单测全是 mock 的，没碰真 PG / 真 RabbitMQ / 真 FastAPI 进程，asyncpg encoder 错、ack 时序错、入口漏注册这些只有真跑才暴露。

现在没有真正的测试环境。所谓 dev/lane 测试都跑在 prod 基建上：DB / MQ / Redis / 飞书 bot 全是 prod 实例，仅靠队列名后缀和路由 lane 隔离。结果是想测一个新东西就要先在脑子里跑一遍"会不会动到 prod 表 / queue / redis key"，每次战战兢兢，最后干脆跳过验证直接部署，回到"上线靠用户踩雷"。

用户已就 v5 framework bug 警告：再出一次直接回滚整个 v5。这件事必须从基建侧解决，光靠纪律和单测兜不住。

## 目标

让 framework bug 在合码前就被真实环境暴露，让破坏性测试可以放手跑而不污染 prod。

不解决：单测质量本身（spec coding / mutation testing 等单独立项）、CI Phase 1/2 升级（pipeline.yml 声明式 + e2e 阶段）、协作过程红线工具化（自动 PR/merge/部署拦截）——这些跟测试环境隔离正交，单独 spec。

## 方案

### lane 分类用命名前缀

lane 注册时 paas-engine 强制校验前缀决定其类别：

- `prod` / `blue`：保留名，paas-engine 蓝绿自部署专用，等同 prod 基建
- `coe-*`（chiwei offline env）：连测试基建，业务测试用
- `ppe-*`（pre prod env）：连 prod 基建，灰度 / AB / dev 类测试用
- 其他无前缀的新 lane 注册一律 reject（fail-closed）
- 历史遗留 lane（如 `dev`）走显式白名单兼容，白名单设过期日期，到期清掉

用命名约定决定环境类别比加 enum 字段干净——名字本身就表明类别，不会出现"字段跟实际行为漂移"。fail-closed 是关键：之前讨论时倾向"无前缀默认按 prod 类对待"是错的，等于"误命名静默打 prod"，跟核心目标完全冲突。

### 测试基建（业务层独立）

K8s 集群本身共享，但业务运行时碰到的所有资源都跟 prod 物理隔离：

- **PG**：独立 docker 容器（`chiwei-test-postgres`）跑在 cpu1，独立卷、独立端口、独立监控。**不共享 prod PG 实例**——契约测试可能制造大事务 / 大 WAL / 大批量临时文件 / autovacuum 风暴 / 磁盘打满，这些是实例级故障，schema 级隔离（role / connection quota / database 隔离）兜不住。codex 在 review 中明确戳穿这点：PG 没有 per-database 磁盘配额这个原生能力，WAL 和 temp file 是实例级共享。
- **RabbitMQ**：同实例新建 `test_vhost`。vhost 级隔离对 MQ 已经够：队列、exchange、user permission、DLX policy、connection 限制全部 vhost 级，互不影响 prod。
- **Redis**：独立 redis 实例。不用 `select db number` —— Redis 多 db 是历史遗留 feature，client 支持参差、Redis Cluster 完全不支持、监控统计混在一起，作者自己说不推荐。独立实例代价跟 docker run 一样小。
- **K8s namespace**：`chiwei-test` ns + ResourceQuota 限 CPU/内存防 test 抢 prod 资源 + NetworkPolicy 防跨 ns 误访问。
- **飞书入口**：写 `mock-feishu` service 跑在 `chiwei-test` ns，提供假的 webhook 端点和假的 send-message 端点。lark-proxy 在 coe-* lane 收到 webhook 时派到 mock-feishu 而不是真飞书。
- **外部 API**（OpenAI / Claude / pixiv 等花钱的）：写对应 mock service 跑在 ns 里，业务 SDK 在 coe-* lane 时 endpoint 指过去。
- **cron / scheduled source**：coe-* lane 默认关 cron（dynamic config 加 `cron_enabled` flag），按需手动触发。否则 test 也跟着每分钟扫 life engine 一遍，浪费且污染。
- **监控告警**：Loki / Prom 共享，按 namespace label 自然隔离 metric 和 log。但告警规则要排除 `chiwei-test` ns，不然 test 跑 chaos 飞书 oncall 群被刷屏。
- **资源熔断 baseline**（codex 第二轮要求的，防清理失效累积污染）：K8s ResourceQuota / PG `statement_timeout` + `idle_in_transaction_session_timeout` / RabbitMQ queue TTL / Redis key TTL，每条 coe lane 默认配上限。

### 多 coe lane 之间互相隔离

多人同时开 coe-* lane 测试时，各 lane 之间不能互相污染：

- PG schema：每个 coe lane 拿一个独立 schema `chiwei_test_<lane_name>`（在独立的 chiwei-test-postgres 实例里）
- RabbitMQ vhost：每个 coe lane 拿独立 vhost `test_vhost_<lane_name>`
- Redis key prefix：每个 coe lane 用独立 key prefix `test:<lane_name>:`
- lane 销毁（undeploy）时 paas-engine 自动清理上述资源

### paas-engine 翻译层

lane 注册和 deploy 两个时机：

1. 注册时：校验 lane 名前缀，记录该 lane 的类别（prod / coe / ppe）。无前缀 reject 或走白名单。
2. Deploy 时：根据 lane 类别派 dynamic config——
   - PG / MQ / Redis 连接串指向对应实例和 schema/vhost/prefix
   - 飞书 SDK 的 base URL 指向真飞书或 mock-feishu service
   - OpenAI / Claude / pixiv SDK 的 base URL 指向真 API 或 mock 对应 service
   - cron 开关按 lane 类别默认值（coe 默认关）

业务代码完全无感、只读 dynamic config 派出来的连接串和 endpoint。

### 上线门禁——framework 契约测试

光有独立测试环境不够，还得有**强制使用**机制——否则 framework bug 还是会被绕过测试直接合码。

每个 coe-* lane 部署完成后，paas-engine 必须触发一组 framework 契约测试套件，覆盖完整 dataflow 执行链路：部署 → 真触发 → 状态推进 → 失败回传 → 清理。**未通过则 release 不算成功**，进而 ship 流程拦住合码。

契约测试覆盖 framework 容易踩雷的几类：

- 所有 durable Data 类做真 INSERT/SELECT round-trip（asyncpg encoder + reserved 字段 + JSONB / list / Decimal / Optional 各类型）
- 所有 wire 真起 consumer 收发一条消息（aio_pika ack 时序、prefetch、process context manager）
- 所有 Source.http 真注册到 FastAPI 进程
- outbox dispatcher 真跑一轮 publish + ack
- DLQ 真 push + replay
- main.py 跟 Runtime.run() 双入口的 hook 注册一致性

### mock 契约漂移检测

mock service 自己也是个隐患——mock 跟真 API 不一致时，"mock 永远绿但 prod 上线即炸"。codex 第二轮把这条优先级从 sub-spec 升级回主 spec 必做项。

mock 实现两条铁律：

1. mock service 用轻量 FastAPI 直写、**不进 runtime/ 目录、不走 dataflow framework** —— 测试替身复制框架缺陷是反模式
2. mock 的 schema / 错误码 / 限流语义必须从真实 SDK fixture 派生，配 prod-mock 契约测试强制对齐。新版 SDK 升级时契约测试 fail，提示 mock 要同步更新。

## 不在本 spec 范围

- spec coding 工作流（plan 列 acceptance criteria + executing-plans 第一个 commit 必须是测试 commit 必 fail）
- mutation testing CI 抓凑数测试
- CI Phase 1（pipeline.yml 声明式）+ Phase 2（e2e 阶段）升级
- 一镜像多 Deployment 自动同步 release（lark-server → 3 个 / agent-service → 2 个）
- 协作过程红线工具化（自动 PR / merge / deploy 拦截 hook）
- memory feedback 治理（rules 拆分 + spec 模板引用）

这些是 dev workflow v2 的其他面，跟测试环境隔离正交，单独立 spec / phase。

## 设计决策记录

**Q：为什么不用 K8s namespace 隔离 + 共享 PG 实例？**
A：codex 两轮 review 反复戳穿。namespace 隔离只解决"业务进程互不见"，PG 实例级故障（WAL / temp file / autovacuum / 磁盘 / CPU）会跨 namespace 传导。chiwei 业务流量小不代表测试压力小——契约测试可能恰好制造大事务和大磁盘场景。

**Q：为什么不用独立 K8s 集群？**
A：早期方向被用户否决——业务层独立就够了，不需要为了 5% 的集群级 chaos 测试常设独立集群。真要测集群级故障临时拉个 k3s 一次性跑就行。

**Q：为什么 lane 分类用命名前缀而不是 enum 字段？**
A：命名约定一眼能看出 lane 类别，不会出现"字段跟实际行为漂移"。前缀写错时 paas-engine 注册阶段就 reject，比 enum 错了静默接受安全。

**Q：为什么 mock service 不复用 dataflow runtime？**
A：mock 的目的是降低对外部依赖的不确定性。如果 mock 走 dataflow，被测 framework 的缺陷会复制到测试替身，mock 自己变成 bug 源。

**Q：为什么飞书和外部 API 全 mock 而不是用 test bot / test API key？**
A：用户明确选 mock 路线。理由：(1) 测试环境聚焦内部基建，外部依赖管 secret 反而把 test 变重；(2) mock 稳定可复现，不会因为 OpenAI 抽风让 e2e 看上去挂了又不知道是谁的问题；(3) chaos 跑外部 API 会烧钱。

## codex review 历程附录

第一轮 codex 给 3 必改：(a) 隔离基建只解决"安全测"没解决"framework bug 必被测到"——必须有契约测试上线门禁；(b) PG 同实例多 database 隔离不够，权限/quota/磁盘/锁等待/扩展全局配置都会传导，建议独立 PG；(c) lane 前缀 fail-open 危险，无前缀默认 prod 类等于"误命名静默打 prod"。

第二轮 Claude 接受 (a)(c)，反驳 (b)（业务量小论据）。codex 推翻反驳：契约测试自身就可能制造大事务/大 WAL/大磁盘，role+quota 是 schema 级 / 兜不住实例级，且 PG 没有 per-database 磁盘配额原生能力。Claude 接受、改成独立 PG docker 容器。同时 codex 第二轮新追加 3 建议（契约测试覆盖完整链路、统一资源熔断、mock 契约漂移检测从 sub-spec 升级），全部并入。
