# chiwei-platform

`MANIFESTO.md`（赤尾宣言）是本项目的宪法，未经 bezhai 明确许可，任何人和任何 AI 不得修改。

## 镜像与服务映射

一个镜像产出多个 Deployment，查日志和排查用实际服务名：

- lark-service → `lark-service`（飞书入站；websocket 只在 prod 建立，只能单副本）、`lark-outbound`（飞书发送和撤回）
- channel-server → `channel-server`（QQ 入站）、`chat-response-worker`（QQ 发送）
- agent-service → `agent-service`（生活引擎）、`world`（独立 App，不是 sibling，deploy 和 logs 都单独用 `APP=world`）

入站只写进 `common_message`，没有队列。agent-service 由定时器触发，每轮查库后自己决定回不回复，代码里没有「收到消息立即回复」的逻辑。

## 配置与部署

- 配置只经 PaaS API 改，不碰 K8s Secret/ConfigMap，分类和语义见 `docs/config-management.md`。改 Release envs 会立即重启 pod。
- 泳道名只接受 `prod`、`blue`（paas-engine 蓝绿专用）、`ppe-*`（共用 prod 的数据和组件）、`coe-*`（独立的 chiwei-test），怎么选见 `.claude/rules/e2e-testing.md`。agent-service 部署到 coe 就会开始跑她的轮次。
- 构建、部署、日志用根目录 `make`，paas-engine 用 `make self-deploy`。Kaniko 从 git remote 拉代码，部署前先 push。部署会重建 Pod，打断正在跑的 rebuild 这类任务，部署前先确认或告诉用户。

## 操作边界

- 直接做，做完报告：分支上 commit、push、跑测试；`ppe-*` / `coe-*` 泳道的部署和 dev bot 绑定（用户正在验收的泳道除外）；读 prod 数据库、日志、Langfuse。
- 先问，每次同意只管那一次，提问或表达意向不算同意：建 PR、合 PR（说「合」才合）、prod 部署和回滚、prod 数据库写入和 DDL、删除有数据的资源、改 prod 配置（含 gateway 规则）、发消息或写外部系统、复制凭据。rebuild 这类批量任务的参数由用户给，不自己填。
- 「直接上线」「跳过用例」是产品上的让步，不包括上线后必然出问题的工程验证（真实并发、真实数据的读取路径），要说明跳过的后果。
- 到集群只有 `$PAAS_API` 一个出口，其他通道会被 hook 拦截：运维用 `/ops`、`/ops-db`，Langfuse 用 langfuse skill。`kubectl exec` 只做只读排查，不取密钥。
- 内网 IP、端口、内部域名、真实姓名不进 git，需要时写「见 memory infrastructure.md」。

## 开发与上线

- 不在 main 上改，分支由用户准备。改动先在泳道验证再上 prod。
- GitHub CLI 用 `ghc`。PR 标题和正文全英文。合并用 `ghc pr merge --squash --subject "..." --body-file <file>`，省掉这两个参数，squash message 会带上 Co-Authored-By 里的邮箱。
- 合并前列出全部 commit 和改动文件，意料之外的文件和冲突交给用户决定；被改函数的所有调用场景（群聊、私聊、主动消息）都要实际跑过，改了写入目标的，读取方也要切换，新表、prompt、schema 要先就绪。
- 代码里不按泳道名判断；可选功能用 Dynamic Config 开关；prod 上跑不到的逻辑不进 main。
- 一次性数据迁移写进服务代码，经 admin endpoint 触发。状态存 DB 表、追加写入，不 UPSERT，核心状态不放 Redis。
- 功能做完再上线，节奏由用户定。不拿发布窗口、分支冲突、prod 没有历史数据当理由催上线。
- 讨论没结束不写 spec；用户质疑设计时，回答设计怎么改，不拿缓解症状代替。

## 赤尾设计原则

- 她的行为不符合预期时，改她的输入（context、prompt、信息、工具、agent 协作），不在逻辑层加确定性规则（阈值、计数器、随机池、格式化函数、if/else）。不确定性是她像人的来源。
- 不在代码或配置里规定这个世界是什么样（作息表、固定钟点、坐标）。读到这类既有机制，先判断它该不该存在。
- 给她的真实输入不截断、不用另一个模型概括；担心 token 就控制条数。
- 她做不到某件事时，先查她的输入里有没有需要的信息（比如消息 id），不用模糊匹配猜她指什么。
- 每轮只给新发生的变化（像手机通知），不重复全量列表或状态快照。
- 她自己的输出累积成的记录（日页、人格版本）会自我强化，长期记忆的证据要用她改写不了的数据（原始消息、别人写的记录）。
- 设计她的功能先从她的视角想她需要看到什么，再推工程实现；不用 room_id、presence 表这类离散结构模拟她的世界。
- 让模型做决定或写入用 function calling，一个决定一个工具，不用正则从自然语言里提取语义。
- 所有 LLM 调用都接 Langfuse trace。
- 赤尾的范式和产品设计不找 codex 评审：它倾向于加入确定性结构，和上面的原则相反。
