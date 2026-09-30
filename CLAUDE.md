# chiwei-platform

`MANIFESTO.md`（赤尾宣言）是本项目的宪法，未经 bezhai 明确许可，任何人和任何 AI 不得修改。

单人维护的 monorepo，应用在 `apps/` 下，部署在 K8s `prod` namespace。

## 服务

```
apps/
  paas-engine/    # PaaS 引擎 (Go)：管理应用构建和部署
  lite-registry/  # 泳道注册表 (Go)：watch K8s Service，提供泳道路由数据
  lark-service/   # 飞书渠道 (Bun/TS)：入站 + 出站
  channel-server/ # QQ 渠道 (Bun/TS)
  qq-gateway/     # QQ 官方 bot 适配 (Bun/TS)：QQ 协议 ↔ channel-server 通用协议
  agent-service/  # 生活引擎 (Python)：由定时源触发运行，每轮自行决定是否发消息
  api-gateway/    # 反向代理入口 (Go)
```

一个镜像会产出多个独立的 Deployment，它们是不同进程、不同 Pod，查日志和排查必须用实际服务名：

| 镜像 | Deployment | 角色 |
|---|---|---|
| lark-service | **lark-service** | 飞书入站：websocket 长连接、webhook 路由、泳道交接接收端（`POST /api/internal/lark/lane-inbound`）、daily-photo / daily-new-photo / emoji-sync 三个定时任务 |
| lark-service | **lark-outbound** | 消费 `chat_response_lark` / `recall_lark`，发飞书消息和撤回 |
| channel-server | **channel-server** | QQ 入站 HTTP（`POST /api/internal/qq/inbound`，由 qq-gateway 投递） |
| channel-server | **chat-response-worker** | 消费 QQ 回复队列，经 qq-gateway 发出 |
| agent-service | **agent-service** | 生活引擎，不消费任何入站队列；另有运维 HTTP（health、admin DLQ） |

飞书消息发送失败查 `make logs APP=lark-outbound`，QQ 查 `APP=chat-response-worker`，不是查 `lark-service` / `channel-server`。

## 消息链路

入站和出站是断开的：

- 飞书入站走 websocket 长连接，不经过 api-gateway。只有 prod 部署且 `LARK_DIRECT_INGRESS=true` 时才建立长连接（单副本，因为飞书对同一 app_id 的多个连接是随机投递）。lark-service 把消息转换成通用格式写进 `common_message`，同时执行飞书指令的规则引擎、判定泳道，入站到此结束，没有队列。`/webhook/{bot}/{event,card}` 仍经 api-gateway 进入，是长连接之外的另一个入口。
- QQ 入站：qq-gateway → channel-server `/api/internal/qq/inbound` → `common_message`，同样到此结束。
- agent-service 不消费入站队列。它每轮运行时查询 `common_message`，决定回复时才把消息发到 `chat_response_lark` / `recall_lark` / `chat_response_qq`，由 lark-outbound / chat-response-worker 发出。代码里没有「收到消息立即回复」的逻辑。

泳道路由：请求带 `x-lane`，lite-registry watch K8s Service 聚合出 `service → {lanes, port}`，LaneRouter SDK（`packages/ts-shared/`、`packages/py-shared/`）拼出 `{app}-{lane}:port`，不存在就回退到 `{app}:port`。这只覆盖入站的 HTTP 交接和出站队列。agent-service 不在任何泳道路由上，`common_message` 也没有 lane 列，共用一个库的两条泳道看到的是同一批消息。泳道测试的细节见 `.claude/rules/e2e-testing.md`。

## 配置

- 基础设施连接和密钥走 ConfigBundle / App envs / Release envs；业务参数（模型、阈值、开关）走 Dynamic Config，运行时由 SDK 读取，10s 缓存。规则、优先级和 API 见 `docs/config-management.md`。
- 一律通过 PaaS API 修改配置，不直接修改 K8s Secret/ConfigMap。查看最终配置：`GET /api/paas/apps/{app}/resolved-config?lane=prod`。`PUT /api/paas/apps/{app}/` 是 merge 语义。
- 改 Release envs 会立即重新部署（pod 重启），改 App envs 不会。

## 泳道

paas-engine 的 `domain.ClassifyLane` 按前缀校验，未知前缀直接拒绝：

| 命名 | 基础设施 | 用途 |
|---|---|---|
| `prod` | 线上 | 生产 |
| `blue` | 共用线上 | 仅供 paas-engine 蓝绿自部署，其他服务禁用 |
| `ppe-<name>` | 共用 prod 全部组件（PG/Redis/MQ/Qdrant/Mongo） | 业务逻辑、prompt 验证；读写的是线上数据 |
| `coe-<name>` | 独立的 chiwei-test 容器集，连接串由 ConfigBundle `class_overrides[coe]` 注入 | schema 变更、协议变更、可能写入错误数据的改动 |

验证 agent-service 的改动只能用 `coe-*`（原因见 e2e-testing.md）。非 prod 泳道默认不启动 cron/interval 定时源，要启动就设 Release env `DATAFLOW_ENABLE_TIME_SOURCES=1`。

## 部署

```bash
make deploy APP=<app> LANE=<lane> GIT_REF=<ref> [BUMP=minor]  # 构建 + 发布，GIT_REF 必须显式写
make release APP=<app> LANE=<lane> VERSION=<x.y.z.w>          # 只发布，不构建（回滚用）
make undeploy APP=<app> LANE=<lane>
make self-deploy [BUMP=minor]                                 # paas-engine 蓝绿自部署（prod ↔ blue）
make status [APP=<app>]
make latest-build APP=<app>
make logs APP=<app> [KEYWORD=... EXCLUDE=... REGEXP=... SINCE=...]
```

- `make` 要在仓库根目录执行，部署类 target 只在根 Makefile 里。
- `deploy` / `release` 会按 `SIBLINGS` 同步发布同镜像的另一个服务（lark-service → lark-outbound，channel-server → chat-response-worker）；`undeploy` 不会，要对它单独再执行一次。
- Kaniko 从 git remote 拉代码，部署前先 push。镜像 tag 由 PaaS 分配。
- 部署会重建 Pod，正在执行的异步任务（rebuild 等）会中断。部署前确认没有这类任务，或者先告诉用户。
- 任何改动先在泳道验证，再部署到 prod，除非用户明确要求直接部署。

## 操作边界

可以直接执行，完成后报告结果：

- 在分支上 commit、push，运行测试。
- `ppe-*` / `coe-*` 泳道的 deploy、release、undeploy、重启，dev bot 绑定和解绑。用户正在验收的泳道除外。
- 读 prod 数据库、查日志、查 Langfuse。

先说明、等用户明确同意再做。每次同意只覆盖那一次操作，不延伸到下一步；用户提问或表达意向不等于同意：

- 创建 PR；合并 PR（用户说「合」才合）；部署、发布或回滚 prod。
- prod 数据库写入和 DDL；删除有数据的资源。
- 通过 PaaS API 改 prod 的配置（ConfigBundle、App / Release envs、Dynamic Config、gateway 规则）。
- 发消息、写外部系统、把凭据复制到其他位置或生成密钥。
- rebuild 这类批量任务的参数（persona、chat_id、时间范围）由用户指定，不自己填默认值、不扩大范围。

用户说「跳过某个用例」「直接上线」是产品层面的让步，不包括上线后必然出问题的工程验证（真实并发、真实数据的读取路径）。这类验证不能跳过，要直接说明跳过后会出什么问题。

硬性限制：

- 开发机到集群只有 `$PAAS_API`（反向代理）一个出口。不要 port-forward、直连 Pod/Service IP、psql、redis-cli，hook 会拦截。运维查询用 `/ops`、`/ops-db`（数据库必须指定 `@chiwei` 或 `@paas_engine`），构建、部署、日志用 `make`，Langfuse 只通过 langfuse skill 操作。已有 skill 能做的事不另写脚本绕过；JSON API 调用优先用 `/api-test` 的 `http.sh`，它不支持的场景（stream、文件、长超时）可以直接 curl。
- `kubectl exec` 只做只读排查；不从 Pod 里取密钥给本地脚本用。
- 内网 IP、端口、内部域名、真实姓名不进 git（代码、文档、commit、PR 都算），需要时写「见 memory infrastructure.md」。
- GitHub CLI 用 `ghc`，不用 `gh`。

## 开发与上线

- 不在 main 上直接修改，分支由用户准备。
- PR 标题和正文全英文，`grep -P '\p{Han}'` 必须为空。合并用 `ghc pr merge --squash --subject "..." --body-file <file>`，不要省略这两个参数，否则默认的 squash message 会带上 `Co-Authored-By` 里的邮箱。一批相关改动合成一个 PR。
- 合并前列出分支上的全部 commit 和改动文件；有意料之外的文件（Makefile、基础设施）先问。rebase / merge 冲突先给用户看，由用户决定取哪边。
- 对外暴露的 API 走 api-gateway 动态规则（`/ops gateway upsert`，先用 `/ops gateway explain` 预览；回滚用 `snapshots` + `rollback`），不改静态 `routes.yaml`。默认不做路径改写，确实需要时单独说明原因。
- 代码里不按泳道名做判断。线上的可选功能用 Dynamic Config 做开关。prod 上执行不到的逻辑不合并进 main。
- 功能全部做完再上线，上线节奏由用户定。本仓库单人维护，不存在并行分支冲突、发布窗口这类问题，不要以此为理由推动上线；新系统在 prod 上没有历史数据是正常的初始状态，也不是阻塞理由。
- 合并前逐项确认：被修改函数的所有调用场景（群聊、私聊、主动消息等）都有运行验证；改了写入目标的，读取方也已切换；新表、新 prompt、schema 变更等副作用都已就绪。
- 一次性的数据迁移、补数据逻辑写进服务代码，通过 admin endpoint 触发；`scripts/` 里不放一次性业务脚本。
- 状态存在可查询的 DB 表里，追加写入保留历史，不用 UPSERT 覆盖；核心业务状态不放 Redis。
- 讨论还没结束时不写 spec；用户质疑设计时，回答设计上怎么改，不要用缓解症状的办法代替设计修改。
- 跟用户说话不用仓库代码注释里的自造比喻（例如「缝」「界桩」「手」「刺激」「信封」「台账」），换成直白描述；代码标识符照写。

## 赤尾设计原则

- 她的行为不符合预期时，改她的输入（context、prompt、给她的信息和工具、agent 协作），不在逻辑层加确定性规则（阈值、计数器、随机池、格式化函数、if/else）。不确定性是她像人的来源，不是 bug。
- 不在代码或配置里规定这个世界是什么样：作息表、固定钟点、坐标这类内容，移到配置里也一样不允许。读到这类既有机制，先判断它该不该存在，再讲它怎么工作。
- 提供给她的真实输入不截断、不用另一个模型概括。担心 token 量就控制条数，不截断单条内容。
- 她做不到某件事时，先检查她的输入里有没有做这件事需要的信息（比如消息 id），不要用模糊匹配去猜她想指什么。
- 每轮给她的只放新发生的变化（像手机通知），不重复放入全量列表或整点状态快照。
- 由她自己的输出累积起来的记录（日页、人格版本）会自我强化；长期记忆的证据来源要用她无法改写的数据（原始消息、别人写的记录）。
- 设计她的功能时，先从她的视角写她会怎么想、需要看到什么，再推导工程实现；不先画 schema / dataflow，不用 room_id、presence 表这类离散结构模拟她的世界。
- 让模型做决定或写入时用 function calling，一个决定一个工具；不用正则从自然语言输出里提取语义。
- 所有 LLM 调用都接 Langfuse trace。
- 赤尾的范式和产品设计不找 codex 评审：它倾向于加入确定性结构，和上面这些原则的方向相反。
