# ops-db 支持 chiwei-test 库

## 目标

让 `/ops-db` skill 能对 chiwei-test（COE 隔离的独立离线 PG 容器集）做只读查询和 submit 变更申请，跟现有 `@chiwei` / `@paas_engine` 体验一致。

## 不做什么

- 不改 ops-db 的审批流程、不改 db-query/db-mutations 端点的协议。
- 不引入"从 ConfigBundle 动态解析连接串"的新机制（已决策走静态 env，复刻 chiwei 模式）。
- 不动其他 COE 基础设施（Redis/MQ/Qdrant/Mongo）。

## 关键设计决策

复刻现有 chiwei 走 `CHIWEI_DATABASE_URL` 环境变量的模式，新增一条独立链路：

1. **客户端 skill（query.py + SKILL.md）**：`DB_ALIASES` 新增 `chiwei-test` 和 `chiwei_test` 两个别名，都映射到 canonical 字符串 `chiwei_test`；SKILL.md 用法文档同步声明 `@chiwei-test`（否则 agent-facing 文档与实际能力不一致）。
2. **服务端 paas-engine**：
   - `config.go` 新增 `ChiweiTestDatabaseURL: os.Getenv("CHIWEI_TEST_DATABASE_URL")`。
   - `main.go` 复刻 chiwei 的两段 block，`cfg.ChiweiTestDatabaseURL != ""` 时往 `opsDbs` / `writeDbs` 注册 key `chiwei_test`（OpenReadOnlyDB / OpenWriteDB）。
   - `SubmitMutation` 增加 db 白名单校验（按 `writeDbs` map 精确校验，未知库 fail fast 返回 400，复刻 Query 的报错逻辑）。canonical 权威在服务端 map，一致性约束必须落在服务端而非只靠客户端归一化——否则任何绕过 skill 的调用或未来 UI 输入会创建审批时才失败的 pending 记录。
3. **部署**：通过 PaaS API 给 paas-engine 注入生产环境变量 `CHIWEI_TEST_DATABASE_URL`，值由 paas_engine 的 `pg-main` config bundle `class_overrides[coe]` 拼出（host/port/db/user/password）；然后 `make self-deploy`。注入位置需明确：查清 paas-engine 现有 `CHIWEI_DATABASE_URL` 来自哪个 config bundle / 哪个 key，把 `CHIWEI_TEST_DATABASE_URL` 放在同一处，避免 secret 加错位置或下次 release 丢失。

## 坑（重点）

1. **客户端 / 服务端 canonical 名字必须逐字节一致。** 服务端是 `h.dbs[dbAlias]` 裸 map 查找，无任何归一化。客户端发什么字符串，服务端就拿什么去查 map。统一定为 `chiwei_test`（下划线）。客户端接受用户输入 `chiwei-test`（连字符，符合泳道命名直觉）和 `chiwei_test`，但发出去的 canonical 一律 `chiwei_test`。一致性约束服务端兜底（见设计决策 2 的 SubmitMutation 校验），不只靠客户端。
2. **`make self-deploy` 先发 prod 再发 blue，没有 blue 预验证闸口。** 不能"blue 先验再 swap prod"。这个改动靠 **fail-safe 设计** 兜底而非预验证：DSN 配错/网络不通时 `OpenReadOnlyDB` 报错，main.go 走 `slog.Warn` 分支跳过注册，paas-engine 本身和现有 `paas_engine`/`chiwei` 链路完全不受影响——最坏情况只是"chiwei-test 仍不可用"，等于现状，不会炸 prod。代价：DSN 配错需改 env + 再 self-deploy（env 在启动时读，必须重启进程才生效），每次迭代都再 hit 一次 prod，但每次都 fail-safe。所以 **尽量一次把 DSN 配对**：从 pg-main coe override 精确拼，部署后立刻查日志确认是否走了 Warn 分支。
3. **网络可达性是最大未知。** chiwei-test PG 在 `10.37.6.235:5433`，COE 业务应用以 coe 泳道 Pod 连它（ConfigBundle 注入）。paas-engine 跑 prod ns，能否直连该地址无法在部署前验证。靠坑 2 的 fail-safe 兜底：连不上只是 Warn 跳过，不破坏 prod。部署后必须查 paas-engine 日志确认 alias 真注册成功（无 `chiwei_test ... unavailable` 的 Warn），不能只看部署成功。
4. **DSN 含内网 IP + 密码,绝不入 git。** spec / 代码 / 测试里只能用假 DSN。真实值只通过 PaaS API env 注入。
5. **部署即杀 Pod。** self-deploy 前确认没有正在跑的 build/rebuild（paas-engine 自身在跑构建时尤其注意，self-deploy 会中断）。
6. **gorm.Open 行为**：复刻 chiwei 即可,连不上不 panic、只 Warn 跳过,降级行为跟 chiwei 一致,无需额外处理。

## 接受的风险（继承自现有 chiwei 模型，本次不扩大）

- **"只读查询"的写保护靠客户端 SQL 正则，不靠 DB 权限。** `OpenReadOnlyDB` 只是普通 gorm 连接、不 AutoMigrate；`CHIWEI_TEST_DATABASE_URL` 用的是 pg-main coe 的 `chiwei_test` 账号（写权限）。只读边界由 query.py 的 `WRITE_KEYWORDS` 正则在客户端拦截，与现有 `@chiwei` 完全同模型。本次复刻不新增风险，也不在本次引入独立只读账号（chiwei 也没有，属另一个独立议题）。

## 任务（粗颗粒）

- **T1 客户端别名 + 文档（TDD）**：query.py `DB_ALIASES` 加 `chiwei-test`/`chiwei_test` → canonical `chiwei_test`；SKILL.md 用法文档同步加 `@chiwei-test`。先写 pytest 覆盖：两种输入都解析到 canonical `chiwei_test`、未知库仍报错、可用库列表含 `chiwei_test`。产出：测试先红后绿 + query.py + SKILL.md 改动。验收：`pytest` 通过（断言归一化后的 db 值为 `chiwei_test`，不依赖真机 curl——`curl -sfS` 的 `-f` 会吞掉 400 body，真机错误文本验证放 T3）。
- **T2 服务端接线 + submit 校验**：config.go 加 `ChiweiTestDatabaseURL`;main.go 复刻 chiwei 两段 block 注册 `chiwei_test` 到 opsDbs/writeDbs;SubmitMutation 加按 writeDbs 的 db 白名单校验（未知库 400 fail fast，复刻 Query 报错逻辑）。产出：Go 改动 + `go build ./...` 通过 + SubmitMutation 校验的 Go 测试（先红后绿）。验收：编译通过 + 测试通过 + code review 确认 map key 与 T1 canonical 逐字节一致。
- **T3 部署 + 真机验证**：先查清 paas-engine 现有 `CHIWEI_DATABASE_URL` 来自哪个 config bundle/key，把 `CHIWEI_TEST_DATABASE_URL`（从 pg-main coe override 精确拼 DSN）放同一处 → PaaS API 注入 → 确认无正在跑的 build → `make self-deploy`。产出：paas-engine 日志确认 chiwei_test alias 注册成功（无 `chiwei_test ... unavailable` Warn）。验收：`/ops-db @chiwei-test SELECT 1` 返回结果;`/ops-db submit @chiwei-test <无害 DDL>` 能提交并审批执行成功。fail-safe 兜底：若日志显示 Warn（DSN/网络问题），prod 不受影响，修正 env 后重新 self-deploy。
