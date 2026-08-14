# 赤尾平台 · 服务拓扑现状

> 最后更新:2026-08-13(飞书拆出 lark-service、recall-worker 下线)。
> 范围:`apps/` 下 15 个应用目录 → 15 个 K8s Deployment(口径:lark-service 一镜像产出 2 个,channel-server 一镜像产出 2 个,lane-sidecar 与 tagger-service 不产出 Deployment,其余 11 个目录各 1 个)+ 1 个注入式 sidecar(lane-sidecar)+ 1 个裸机 GPU 服务(tagger-service,不进 K8s)+ `packages/` 4 个共享包。
> 这是**现状**梳理,不含目标架构和改造方案。术语在文中随用随解释。

---

## 一、这个平台到底是什么

一句话:这是一个跑在 K8s `prod` namespace 的 monorepo,核心业务是让虚拟人「赤尾(三姐妹)」在飞书(以及 QQ)里像真人一样聊天和自主活动。围绕这个核心,平台自己还长出了一整套**部署系统**(自建 PaaS)、**运维后台**、和**泳道路由**(让一份代码能并行跑多套隔离环境用于测试)。

这些服务并不在同一个层面,它们分属五个不同的「面」:

- **数据面**:真正处理消息、跑 AI 对话的链路。
- **AI 工具后端**:agent 推理时会调用的外部能力(执行代码、处理图片)。
- **控制面**:管构建、部署、配置的自建 PaaS + 运维后台。
- **网络基础设施**:让请求能按「泳道」路由到不同环境的三件套。
- **旁路**:告警转发、媒体素材同步与 GPU 打标,跟主链路没有实时耦合。

把它们按面分清楚,是看懂这个拓扑的第一步——很多「这个服务为什么存在」的困惑,都是因为把不同面的东西混在一张图里看。

---

## 二、服务全景

外部流量(运维浏览器、开发机、QQ 回调)统一从 **api-gateway** 进集群,它按规则把请求分流到对应服务,并盖上泳道 header。**飞书入站是唯一的例外**:lark-service 主动持 websocket 长连到飞书开放平台,事件由飞书推过来,不经 api-gateway(webhook 路由仍然注册着,是长连之外的被动入口)。

```mermaid
flowchart TB
    Feishu["飞书 / Lark"]
    QQ["QQ 开放平台"]
    Browser["运维浏览器"]
    Prom["Prometheus<br/>AlertManager"]
    LLM["大模型<br/>Gemini / GPT 等"]

    GW["api-gateway<br/>外部统一入口 · 按规则分流 + 盖 lane header"]

    subgraph data["数据面 · 消息处理 + AI 对话"]
        LKS["lark-service<br/>飞书入站:长连 + webhook + 投影 + 规则指令"]
        LKO["lark-outbound<br/>飞书出站:发回复 + 撤回"]
        QGW["qq-gateway<br/>QQ 协议 ↔ 通用协议"]
        CS["channel-server<br/>QQ 入站 + 渠道核心 + 规则引擎"]
        AS["agent-service<br/>AI 对话 + world/life 引擎"]
        CRW["chat-response-worker<br/>QQ 出站:发回复"]
    end

    subgraph tools["AI 工具后端 · 被 agent 调用"]
        SB["sandbox-worker<br/>隔离 bash 执行"]
        TS["tool-service<br/>图像管道 + 关键词"]
    end

    subgraph ctrl["控制面 · 自建 PaaS + 运维"]
        DW["monitor-dashboard-web<br/>运维前端 SPA"]
        DB["monitor-dashboard<br/>BFF + 审计"]
        PE["paas-engine<br/>构建 / 部署 / 配置控制面"]
    end

    subgraph net["网络基础设施 · 泳道路由"]
        REG["lite-registry<br/>泳道服务发现"]
        LS["lane-sidecar<br/>每个 pod 注入的透明代理"]
    end

    subgraph aside["旁路 · 与主链路无实时耦合"]
        AW["alert-webhook<br/>告警转飞书"]
        MS["media-sync-worker<br/>Pixiv / Bangumi 素材同步"]
        TAG["tagger-service<br/>GPU 打标(裸机)"]
    end

    Feishu -.->|websocket 长连| LKS
    QQ -->|回调| GW
    Browser -->|/dashboard| GW
    GW --> QGW --> CS
    GW --> DW
    GW --> DB --> PE
    LKS -. chat_request .-> AS
    CS -. chat_request .-> AS
    AS -. chat_response_lark / recall_lark .-> LKO --> Feishu
    AS -. chat_response_qq .-> CRW --> QGW -->|发消息| QQ
    AS --> SB
    AS --> TS
    AS --> LLM
    Prom --> AW --> Feishu
    MS --> TAG
    REG -. 下发路由表 .-> LS
```

虚线箭头是 RabbitMQ 消息队列(异步,飞书那条长连除外),实线是直接 HTTP 调用。注意 agent-service 不直接发平台消息——它把回复丢进队列,由持有平台凭证的那个服务代发:飞书归 lark-outbound,QQ 归 chat-response-worker(再经 qq-gateway)。lane-sidecar / lite-registry 不在某条线性调用链上,它们横切所有服务间调用(见第五节)。tagger-service 是图里唯一不跑在 K8s 上的服务:裸机 GPU 主机 + systemd 托管,media-sync-worker 通过 HTTP 提交打标任务、用回调收结果。

---

## 三、核心数据流:一条消息的旅程

这是整个平台最重要的一条链路。一个用户在飞书 @ 了赤尾,到她回话,中间发生了什么:

```mermaid
sequenceDiagram
    participant U as 飞书用户
    participant LKS as lark-service
    participant MQ as RabbitMQ
    participant AS as agent-service
    participant LKO as lark-outbound

    U->>LKS: 事件经 websocket 长连推过来
    LKS->>LKS: 解析,收敛 common 口径并决定 lane
    Note over LKS: 渠道契约链:解析→判定是否响应→换全局身份→规则引擎→存储
    LKS->>MQ: publish chat_request(带 channel + 全局 ID)
    MQ->>AS: 消费 chat_request
    AS->>AS: 组装意识(人格/状态/记忆) + LLM 推理 + 工具调用
    AS->>MQ: publish chat_response_lark(逐段流式)
    MQ->>LKO: 消费 chat_response_lark
    LKO->>U: 反查回飞书裸 ID,发送 / 追加回复
```

QQ 那条链形状相同,只是入站是 qq-gateway 把 QQ 协议归一化成 `CustomInboundMessage` 后 POST 给 channel-server,出站由 chat-response-worker 消费 `chat_response_qq` 再回投 qq-gateway。

三个服务各自的角色,用人话说:

- **lark-service** 是飞书渠道的全部:入站(长连 + webhook)、common 投影、规则与指令、以及三个定时任务(daily-photo / daily-new-photo / emoji-sync)。入站走一条钉死顺序的契约链——解析 → 收敛成 common 口径 → 平台无关的规则引擎分发 → 存消息 → 发 `chat_request`。出站在同镜像的另一个 Deployment **lark-outbound** 里:它消费 `chat_response_lark` / `recall_lark` 两条队列,把话送到飞书、把判违规的撤掉。拆成两个进程是因为部署策略冲突——持长连的只能单副本 + Recreate,出站是竞争消费、可多副本可滚动更新。
- **channel-server** 是 QQ 渠道的同类角色(入站 HTTP 入口 + 渠道核心 + 规则引擎),出站在 chat-response-worker。
- **agent-service** 是「大脑」。它消费 `chat_request`,把赤尾的人格、当前状态、相关记忆「组装」成上下文喂给大模型,用自研的 agent 工具循环驱动推理(不依赖 langchain 之类的框架),推理过程中可以调工具(搜索、画图、找图、执行代码、技能脚本),最后把回复分段流式地丢回 `chat_response` 队列。

除了「回复」这条主线,赤尾还有自己的后台生活(细节见 `docs/chiwei-system-design.md`,这里不展开):**world/life 引擎**(world 按自己定的节奏推演世界,每个角色的 life agent 自主安排并执行生活)和**记忆沉淀**(会话转写沉淀 + 睡前回顾把一天压成记忆页)。这些都跑在 agent-service 主进程里,由 dataflow runtime 驱动。

---

## 四、RabbitMQ 队列地图

跨服务的异步通信全靠 RabbitMQ。入站方是各渠道服务,出站方是 agent-service,消费方是各渠道自己的出站进程。

**出站队列按 channel 分区**:队列名和 routing key 都揉进 channel(`chat_response` → `chat_response_lark`,`chat.response` → `chat.response.lark`)。分区维度必须跟消费者的所有权维度一致——飞书的回复只能由持飞书凭证的 lark-outbound 发,一条都不能被别的服务领走。入站的 `chat_request` 不分区:消费者只有 agent-service 一个。

```mermaid
flowchart LR
    LKS["lark-service"]
    CS["channel-server"]
    AS["agent-service"]
    LKO["lark-outbound"]
    CRW["chat-response-worker"]

    LKS ==>|chat_request| AS
    CS ==>|chat_request| AS
    AS ==>|chat_response_lark| LKO
    AS ==>|recall_lark| LKO
    AS ==>|chat_response_qq| CRW
```

| 队列 | 生产者 | 消费者 | 干什么 |
|---|---|---|---|
| `chat_request` | lark-service / channel-server | agent-service | 「请赤尾回这条消息」 |
| `chat_response_lark` | agent-service | lark-outbound | 「这是赤尾的回复,帮我发飞书」 |
| `recall_lark` | agent-service(安全审核后) | lark-outbound | 「刚那条要撤回」 |
| `chat_response_qq` | agent-service | chat-response-worker | 「这是赤尾的回复,帮我发 QQ」 |
| `inbound_lane.{channel}.{lane}` | 各渠道服务的 prod 实例 | 同渠道服务的泳道实例 | 「这条消息该归你那个泳道处理」 |

agent-service 内部还有一批异步事件(比如 `CommonMessageContentSynced`——消息里的图片落 TOS 后回写消息记录)走 dataflow runtime 的 durable 节点,底下的 RabbitMQ 队列由 runtime 框架按 Data 类型声明和管理,不在上表逐一列出。另有 `proactive_eval` 队列声明了但**没有任何生产者和消费者**,是死队列。

所有队列都带泳道后缀(`xxx_<lane>`),泳道队列有 10s TTL,过期后消息降级回 prod 队列——这保证了未部署泳道的服务能 fallback 到线上。

---

## 五、泳道路由怎么工作

「泳道(lane)」= 一套并行的隔离环境,用一个 header `x-ctx-lane` 标识。同一份代码可以部署成 `agent-service`(prod)、`agent-service-ppe-x`(测试泳道)等多个实例,请求带不同的 lane header 就会被路由到不同实例;某个服务没部署对应泳道时,自动落回 prod。

实现这件事靠三个服务 + 一个 SDK 配合:

```mermaid
flowchart TB
    App["业务服务 A<br/>(代码里用 LaneRouter SDK)"]
    LS["lane-sidecar<br/>(同 pod, iptables 透明拦截出站流量)"]
    REG["lite-registry"]
    K8s["K8s API"]
    Target["服务 B 的泳道实例<br/>agent-service-ppe-x"]

    App -->|"① 出站请求, SDK 注入 x-ctx-lane<br/>(iptables 重定向到 localhost:15001)"| LS
    LS -->|"② 读 header + 查路由表, 改写服务名<br/>agent-service → agent-service-ppe-x"| Target
    LS -. "③ 30s 轮询 /v1/routes" .-> REG
    App -. "也轮询拿路由表" .-> REG
    REG -. "watch K8s Services" .-> K8s
```

- **lite-registry** 是泳道路由的「真值源」。它 watch K8s 里所有 Service,聚合成一张表:每个服务名 → 它在哪些泳道有部署、端口是多少。对外只提供 `GET /v1/routes`。
- **lane-sidecar** 是被注入到**每个业务 pod** 里的透明代理(不是独立 Deployment,是个 sidecar 容器,由 paas-engine 在部署时注入)。它用 iptables 把 pod 所有出站 TCP 劫持到自己,读请求里的 `x-ctx-lane`,把目标服务名改写成带泳道后缀的名字。业务代码完全无感知。
- **LaneRouter SDK**(在 `ts-shared` 和 `py-shared` 各一份)是应用层的配合件:负责在发请求时注入 `x-ctx-lane` header。有了 sidecar 之后,**真正的服务名改写挪到了 sidecar 的网络层**,SDK 不再自己拼泳道后缀,只管注 header。两者是互补,不是重复。
- **api-gateway** 是从集群**外部**进来的反向代理入口(开发机到集群的唯一出口)。它轮询 paas-engine 下发的网关规则,按路径前缀匹配,选中目标后转发,并盖上 `x-ctx-lane` header。它管的是「外→内」的入口路由,sidecar 管的是「内→内」的服务间路由。

---

## 六、控制面与运维链路

这条线和飞书 / AI 完全无关,是平台自己的「基础设施管理」:构建镜像、蓝绿部署、改配置、看状态、查库。

```mermaid
flowchart LR
    Browser["浏览器(运维)"]
    GW["api-gateway"]
    DW["monitor-dashboard-web<br/>(Nginx + React SPA)"]
    DB["monitor-dashboard<br/>(BFF + 审计落库)"]
    PE["paas-engine<br/>(真正的控制面)"]
    Infra["K8s / Harbor / Loki / 业务库"]
    Audit["审计日志库"]

    Browser --> GW
    GW -->|静态资源| DW
    GW -->|"/dashboard/api/*"| DB --> PE --> Infra
    DB -->|每次写操作| Audit
```

- **paas-engine** 是这条线的核心,自建 PaaS 引擎。它本职是**构建**(用 Kaniko 在 K8s 里跑构建 Job,推 Harbor)和**部署**(创建 K8s Deployment + Service,支持蓝绿)。但它还累积了不少别的职责:网关规则的增删改查、动态配置(运行时下发给业务 SDK 的模型/阈值/开关)、ConfigBundle(部署时的环境变量集)、CI 流水线、日志查询(Loki),以及一个能对**业务库**跑 SQL 和 DDL/DML 审批的 ops 网关。
- **monitor-dashboard** 是无状态的 **BFF(给前端用的后端)+ 审计网关**。它本身不做任何控制决策,只是:校验授权 → 把请求转发给 paas-engine(或 channel-server 做泳道绑定)→ 把每次写操作记进审计日志库。它存在的核心价值是「统一审计入口」和「给前端收口」。
- **monitor-dashboard-web** 是纯静态 React SPA,Nginx 托管,把 `/dashboard/api/*` 反代到 api-gateway。

---

## 七、部署拓扑:一镜像多服务

一个 Docker 镜像可以产出多个独立的 K8s Deployment(不同进程、不同 pod)。这是排查问题时最容易踩坑的地方——查 lark-outbound 的日志不能用 lark-service 的服务名,查 chat-response-worker 的日志不能用 channel-server 的服务名。全平台共 15 个 K8s Deployment:

| 镜像 | 产出的 Deployment | 角色 |
|---|---|---|
| lark-service | **lark-service** | 飞书入站:websocket 长连 + webhook 路由 + 泳道信封消费 + 三个定时任务 |
| lark-service | **lark-outbound** | 消费 `chat_response_lark` / `recall_lark`,发飞书回复与撤回 |
| channel-server | **channel-server** | HTTP,QQ 入站(`POST /api/internal/qq/inbound`) |
| channel-server | **chat-response-worker** | 消费 `chat_response_qq`,经 qq-gateway 发 QQ 回复 |
| agent-service | **agent-service** | 单 Deployment:HTTP(健康检查 + admin)+ chat 消费 + dataflow durable 节点 + world/life 引擎 |
| 其余 11 个 | 各自 1 个同名 Deployment | — |

15 = lark-service 2 + channel-server 2 + 其余 11 个目录各 1。两个不在此表的例外:`lane-sidecar` 不是独立 Deployment,而是注入到上面每个业务 pod 里的容器;`tagger-service` 完全不在 K8s 里,跑在裸机 GPU 主机上由 systemd 托管。这两个目录不产出 Deployment,所以 15 个应用目录对应 15 个 Deployment。

---

## 八、数据存储归属

谁连哪个库,看清楚有助于理解「改了某张表会影响哪些服务」:

| 存储 | 谁在用 |
|---|---|
| PostgreSQL · 业务库(chiwei) | lark-service / lark-outbound、channel-server / chat-response-worker、agent-service、tool-service、monitor-dashboard、qq-gateway,以及 paas-engine 的 ops 网关(读 + DDL/DML 审批) |
| PostgreSQL · paas_engine 库 | paas-engine 自己 |
| MongoDB | lark-service(飞书原始报文 `lark_event`)、media-sync-worker(媒体)、monitor-dashboard。**channel-server 拆分后不再连 Mongo** |
| Redis | lark-service、lark-outbound(图片注册表)、channel-server、chat-response-worker(图片注册表)、agent-service、tool-service、media-sync-worker、qq-gateway |
| TOS(对象存储) | tool-service(图像管道上传);lark-service / channel-server / agent-service 只在消息记录里传递 TOS 文件名,不直连 |
| MinIO(对象存储) | media-sync-worker(素材入库)、tagger-service(打标取图)、lark-service(本地 pixiv 图源) |
| Harbor(镜像仓库) | paas-engine(Kaniko 构建产物) |
| K8s API | paas-engine、lite-registry、lane-sidecar |

---

## 九、服务职责速查表

| 服务 | 栈 | 面 | 一句话职责 |
|---|---|---|---|
| lark-service | Bun/TS | 数据面 | 飞书入站(长连 + webhook)+ 渠道契约链 + 规则指令 + 定时任务,决定是否触发 AI |
| lark-outbound | Bun/TS | 数据面 | 消费 `chat_response_lark` / `recall_lark`,发飞书回复与撤回 + 存储 |
| channel-server | Bun/TS | 数据面 | QQ 入站 + 渠道契约链 + 规则引擎 + 存储,决定是否触发 AI |
| chat-response-worker | Bun/TS | 数据面 | 消费 `chat_response_qq`,经 qq-gateway 发 QQ 回复 + 存储 |
| qq-gateway | Bun/TS | 数据面 | QQ 官方 bot 协议 ↔ channel-server 通用协议的双向适配 |
| agent-service | Python | 数据面 | AI 对话引擎(自研 agent 工具循环 + dataflow runtime)+ world/life 引擎 + 记忆沉淀 |
| sandbox-worker | Python | AI 工具 | 隔离环境跑 bash / 技能脚本 |
| tool-service | Python | AI 工具 | 图像管道(下载→压缩→TOS)+ jieba 关键词 |
| paas-engine | Go | 控制面 | 构建+部署+网关规则+动态配置+CI+日志+业务库 ops |
| monitor-dashboard | Bun/TS | 控制面 | 运维 BFF + 审计落库,转发 paas-engine |
| monitor-dashboard-web | React | 控制面 | 运维前端 SPA |
| api-gateway | Go | 网络基建 | 外部反向代理入口,按规则分流 + 盖 lane header |
| lite-registry | Go | 网络基建 | watch K8s Service,提供泳道路由真值表 |
| lane-sidecar | Go | 网络基建 | 注入每 pod,透明改写出站服务名做泳道路由 |
| alert-webhook | Go | 旁路 | Prometheus 告警转飞书 |
| media-sync-worker | Bun/TS | 旁路 | 定时从 Pixiv/Bangumi 同步素材到 MongoDB / MinIO,并驱动 tagger-service 打标 |
| tagger-service | Python | 旁路 | Pixiv 图片 GPU 打标管线(裸机 systemd,不进 K8s) |

**共享包(不部署)**:`ts-shared`(TS 中间件/缓存/日志/HTTP/MongoDB/LaneRouter SDK/实体)、`py-shared`(Python 同类基建 + LaneRouter SDK + 动态配置)、`lark-utils`(飞书 SDK 封装)、`pixiv-client`(Pixiv API client)。

---

## 十、现状里值得重新设计的点

以下是梳理时观察到的问题,**按重要性排序**。(2026-06-01 版列在最前的「channel 多渠道抽象泄漏」和「身份迁移双状态」两点,已分别被 channel-server 平台无关重构(PR #244:core 不再 import 平台 SDK,飞书代码全部收进 `plugins/lark/`)和 common identity 层迁移(commit 448e0fe,agent-service 不再解析 app_id/open_id)解决,不再列出。)

### 1. agent-service 主进程承担过多

它同时是 chat 的 MQ 消费者、admin/DLQ 管理 HTTP、一批 dataflow durable 节点,还是 world/life 自主生活引擎的宿主。面向用户的对话延迟,和后台自主行为,挤在同一个 Deployment 里抢资源。

### 2. paas-engine 是个「全能控制面」

build/release 是本职,但它还累积了网关规则、动态配置、ConfigBundle、CI、日志、以及对业务库跑 SQL/DDL 的 ops 网关。多数(配置类)自洽,唯一像跑错地方的是业务库 ops 控制台。

### 3. 杂项

跨语言队列契约各写一遍(TS 的 `rabbitmq.ts` 和 Python 的 wiring 各一份,有漂移风险);`proactive_eval` 是死队列;`CLAUDE.md` 的项目结构只列常用的几个 app(全量 15 个目录的清单在根 README 和本文档)。
