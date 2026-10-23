# chiwei-platform

`MANIFESTO.md`（赤尾宣言）是本项目的宪法，未经 bezhai 明确许可，任何人和任何 AI 不得修改。

单人维护的 monorepo，应用在 `apps/` 下，部署在 K8s `prod` namespace。服务拓扑见 `docs/service-topology.md`。

## 镜像与服务映射

同一个镜像产出的 Deployment 是不同进程、不同 Pod，查日志和排查必须用实际服务名：

| 镜像 | Deployment | 职责 |
|---|---|---|
| lark-service | **lark-service** | 飞书入站（websocket 长连接、webhook、泳道交接接收端）和几个定时任务 |
| lark-service | **lark-outbound** | 发飞书消息和撤回。飞书发送失败查这里 |
| channel-server | **channel-server** | QQ 入站，由 qq-gateway 投递 |
| channel-server | **chat-response-worker** | 经 qq-gateway 发 QQ 消息。QQ 发送失败查这里 |
| agent-service | **agent-service** | 生活引擎 |
| agent-service | **world** | 独立 App，不是 sibling：部署用 `make deploy APP=world`，日志用 `make logs APP=world` |

## 消息链路

入站和出站是断开的。lark-service、channel-server 把入站消息转换成通用格式写进 `common_message` 就结束，没有入站队列。agent-service 由自己的定时器触发，每轮查询 `common_message`，决定回复时才发到 `chat_response_*` / `recall_*` 出站队列。代码里没有「收到消息立即回复」的逻辑。

飞书 websocket 长连接只在 prod 建立，而且只能单副本：飞书对同一 app_id 的多个连接是随机投递的。泳道路由、交接和回退见 `.claude/rules/e2e-testing.md`。

## 配置

- 基础设施连接和密钥走 ConfigBundle / App envs / Release envs，业务参数（模型、阈值、开关）走 Dynamic Config，详见 `docs/config-management.md`。
- 只通过 PaaS API 改配置，不直接改 K8s Secret/ConfigMap。`PUT /api/paas/apps/{app}/` 是 merge 语义；查最终配置用 `GET /api/paas/apps/{app}/resolved-config?lane=prod`。
- 改 Release envs 会立即重新部署（pod 重启），改 App envs 不会。

## 泳道

paas-engine 按前缀校验泳道名，未知前缀直接拒绝：`prod` 是生产；`blue` 只给 paas-engine 蓝绿自部署用；`ppe-<name>` 共用 prod 的全部组件，读写的是线上数据；`coe-<name>` 用独立的 chiwei-test 容器集，适合 schema 变更、协议变更和可能写错数据的改动。

验证 agent-service 的改动只能用 coe（原因见 e2e-testing.md）。非 prod 泳道默认不启动她的定时器，但 ConfigBundle `agent-service-runtime` 对 coe 把 `DATAFLOW_ENABLE_TIME_SOURCES` 设成了 1，所以部署到 coe 就会开始跑她的轮次。world 不读这个开关，收件箱里有消息就会运行。

## 部署

```bash
make deploy APP=<app> LANE=<lane> GIT_REF=<ref> [BUMP=minor]  # 构建 + 发布；GIT_REF 显式写，prod 只能是 main
make release APP=<app> LANE=<lane> VERSION=<x.y.z.w>          # 只发布不构建，回滚用
make undeploy APP=<app> LANE=<lane>
make self-deploy [BUMP=minor]                                 # paas-engine 蓝绿自部署（prod ↔ blue）
make logs APP=<app> [KEYWORD=... SINCE=... POD=...]
```

- 在仓库根目录执行。Kaniko 从 git remote 拉代码，部署前先 push。
- deploy / release 会同步发布同镜像的 sibling（lark-outbound、chat-response-worker），undeploy 不会，要对 sibling 单独执行。
- 部署会重建 Pod，正在执行的异步任务（rebuild 等）会中断。部署前确认没有这类任务，或者先告诉用户。

## 操作边界

可以直接做，做完报告结果：

- 在分支上 commit、push，运行测试。
- `ppe-*` / `coe-*` 泳道的 deploy、release、undeploy、重启，dev bot 绑定和解绑。用户正在验收的泳道除外。
- 读 prod 数据库、查日志、查 Langfuse。

先说明、等用户明确同意再做。每次同意只覆盖那一次操作，不延伸到下一步；用户提问或表达意向不等于同意：

- 创建 PR；合并 PR（用户说「合」才合）；部署、发布或回滚 prod。改动先在泳道验证再上 prod，除非用户明确要求直接上。
- prod 数据库写入和 DDL；删除有数据的资源。
- 通过 PaaS API 改 prod 配置（ConfigBundle、App / Release envs、Dynamic Config、gateway 规则）。
- 发消息、写外部系统、把凭据复制到其他位置或生成密钥。
- rebuild 这类批量任务的参数（persona、chat_id、时间范围）由用户指定，不自己填默认值、不扩大范围。

用户说「跳过某个用例」「直接上线」是产品层面的让步，不包括上线后必然出问题的工程验证（真实并发、真实数据的读取路径）。这类验证不能跳过，要直接说明跳过后会出什么问题。

硬性限制：

- 开发机到集群只有 `$PAAS_API` 一个出口，port-forward、直连 Pod/Service IP、psql、redis-cli 会被 hook 拦截。运维查询用 `/ops`、`/ops-db`，构建、部署、日志用 `make`，Langfuse 只用 langfuse skill，JSON API 优先用 `/api-test`（stream、文件、长超时这类场景可以直接 curl）。已有 skill 能做的事不另写脚本绕过。
- `kubectl exec` 只做只读排查，不从 Pod 里取密钥给本地脚本用。
- 内网 IP、端口、内部域名、真实姓名不进 git（代码、文档、commit、PR 都算），需要时写「见 memory infrastructure.md」。
- GitHub CLI 用 `ghc`，不用 `gh`。

## 开发与上线

- 不在 main 上直接修改，分支由用户准备。
- PR 标题和正文全英文，`grep -P '\p{Han}'` 必须为空。合并用 `ghc pr merge --squash --subject "..." --body-file <file>`，两个参数都不能省，否则默认的 squash message 会带上 `Co-Authored-By` 里的邮箱。一批相关改动合成一个 PR。
- 合并前列出分支上的全部 commit 和改动文件，有意料之外的文件（Makefile、基础设施）先问。rebase / merge 冲突交给用户决定取哪边。
- 合并前确认：被修改函数的所有调用场景（群聊、私聊、主动消息等）都有运行验证；改了写入目标的，读取方也已切换；新表、新 prompt、schema 变更等副作用都已就绪。
- 对外暴露的 API 走 api-gateway 动态规则（`/ops gateway upsert`，先用 `explain` 预览，回滚用 `snapshots` + `rollback`），默认不做路径改写。
- 代码里不按泳道名做判断。线上的可选功能用 Dynamic Config 做开关。prod 上执行不到的逻辑不合并进 main。
- 一次性的数据迁移、补数据逻辑写进服务代码，通过 admin endpoint 触发，不放进 `scripts/`。
- 状态存在可查询的 DB 表里，追加写入保留历史，不用 UPSERT 覆盖；核心业务状态不放 Redis。
- 功能全部做完再上线，节奏由用户定。单人维护不存在并行分支冲突、发布窗口这类问题，新系统在 prod 上没有历史数据是正常的初始状态，这些都不是推动上线的理由。
- 讨论还没结束时不写 spec；用户质疑设计时，回答设计上怎么改，不用缓解症状的办法代替设计修改。

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
