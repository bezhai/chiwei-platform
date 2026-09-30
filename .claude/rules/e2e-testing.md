# 飞书 dev 泳道端到端测试

## 核心原则

测试飞书相关链路时，要把 dev bot 绑定到目标泳道。**改了哪个服务就部署哪个服务，其他服务不需要一起部署**：没有部署到泳道的服务，流量会自动回退到 prod 实例（入站由 lane-sidecar 回退，出站由队列 TTL 回退，见下文）。反过来，要验证的改动在哪个服务里，那个服务就必须部署到泳道，否则实际运行的是 prod 的代码，而且从现象上看不出来。

## 泳道选择

**验证 agent-service 的改动只能用 `coe-<name>`。** 入站已经没有按泳道划分的队列，agent-service 读取的 `common_message` 也没有 lane 列，一条消息被谁处理，只取决于哪个进程在读这个库。放在 ppe 上只有两种结果：一是泳道的 agent-service 默认不启动定时源（`time_sources_enabled_by_default` 对非 prod 返回 False），它根本不运行，实际验证的是 prod 的代码；二是用 `DATAFLOW_ENABLE_TIME_SOURCES=1` 打开定时源，prod 和泳道两个 agent-service 读同一批未读消息、各自回复，**线上的真实用户会收到两条回复**。coe 用独立的数据库，消息写在 chiwei-test 里，和 prod 天然隔离。

验证 lark-service / channel-server / lark-outbound 自身的改动（入站格式转换、规则引擎、出站投递）两种泳道都可以，这几个服务仍然按泳道路由：

- **`ppe-<name>`（共用 prod 组件）**：表结构、历史数据、种子配置都用线上的，不需要准备。代价是 dev bot 触发的所有写入（消息记录、撤回、新表新字段）都直接写进 prod，schema 变更或错误数据会污染线上历史；而且消息写进的是 prod 库，所以**回复消息的是 prod 的 agent-service**。适合入站格式转换、规则引擎、出站投递这类不改数据库、也不涉及 agent-service 的改动。
- **`coe-<name>`（独立的 chiwei-test 容器集）**：写入只影响 chiwei-test，不会波及 prod。代价是要**提前准备 chiwei-test 的数据**：
  - **schema**：agent-service 启动时，framework 的 Data 表（`data_*`）由 `app/runtime/migrator.py` 增量创建；老的 SQLAlchemy ORM 表只在 coe-* 泳道由 `ensure_business_schema()` 用 `create_all` 创建。不在这两处注册的新表或新字段不会被创建。
  - **种子数据**：dev bot 运行必须读到的 user / persona / bot 配置等，要从 prod 导出一份到 chiwei-test 对应的库。
  - 适合 schema 变更、消息协议变更、写入量很大或可能写入错误数据的改动。

## 标准流程

1. 把改动的服务部署到独立泳道：`make deploy APP=<app> LANE=<lane> GIT_REF=<ref>`（`<lane>` 按上面的规则选 `ppe-<name>` 或 `coe-<name>`）。lark-service 和 lark-outbound 是同一镜像的两个 Deployment，部署 lark-service 会自动把 lark-outbound 发布到同一泳道，所以改其中任何一个都用 `APP=lark-service`。
2. 如果用 coe：确认 schema 已创建、必要的种子数据已复制到 chiwei-test。
3. 绑定 dev bot：`/ops bind TYPE=bot KEY=dev LANE=<lane>`。
4. 在飞书里给 dev bot 发消息验证。
5. 验证完毕后清理：
   - `/ops unbind TYPE=bot KEY=dev`
   - `make undeploy APP=<app> LANE=<lane>`
   - **部署过 lark-service 的，还要对 `APP=lark-outbound` 再执行一次 undeploy**。`undeploy` 不像 `deploy` / `release` 那样同步处理同镜像的另一个服务（Makefile 里只有这两个 target 有循环），只删除指定的那一个。遗漏的 `lark-outbound-<lane>` 会在下次测试时继续消费该泳道的出站队列，让本该由 prod 处理的出站看起来验证通过。

## 消息流转链路

飞书入站通过 lark-service 的 websocket 长连接接收，**只有 prod 部署会建立长连接**（条件是 `isProdDeployment() && LARK_DIRECT_INGRESS === 'true'`）。泳道部署不连 websocket，消息由 prod 通过一次内部 HTTP 请求转发过来，代码和日志里把这次转发叫 handoff（交接）：

```
飞书 --websocket--> lark-service(prod)
  → 转换成通用格式，LaneBindingResolver 查 lane_routing 表（会话绑定优先，bot 绑定其次）
  → 命中泳道 X：带 header x-ctx-lane: X 发送 POST /api/internal/lark/lane-inbound
      （QQ 侧结构相同，路径是 /api/internal/qq/lane-inbound）
  → lane-sidecar 按 header 查 lite-registry，把目标 lark-service:3000 改写成 lark-service-X:3000
  → lark-service(X) 收到交接请求体（代码里叫 envelope），用其中的 lane 建立上下文继续处理
      （原始报文已经在 prod 那一侧记录过审计，这里不重复写入）
  → 转换成通用格式写进 common_message，入站到此结束，没有队列

agent-service(X) 由自己的定时源触发运行，每轮查询 common_message 时才读到这条消息
      （前提是 X 是 coe：ppe 共用 prod 库，读到它的是 prod 的进程，见上面的泳道选择）
  → 决定回复 → chat_response_lark_X 队列 → lark-outbound(X) → 飞书
```

交接请求的目标服务名就是 `lark-service`，泳道后缀由 sidecar 按 `x-ctx-lane` 改写，业务代码里没有路由逻辑。这次请求**不重试**：飞书那边早已收到应答，重试就等于把同一条消息处理两遍。

出站仍然走 MQ：`chat_response_lark_{lane}` 队列设置了 10s TTL，过期后通过 DLX 转回 prod 队列。

没有绑定泳道的消息（包括未绑定时的 dev bot）全程由 prod 处理。

## 未部署的服务如何回退到 prod

入站和出站各有一个回退机制，所以「只部署改动的服务」才可行：

- **入站**：泳道的 K8s Service 不存在时，sidecar 把交接请求原样转回 prod，由 prod 的代码转换格式并写入数据库。所以绑定到一条没有部署 lark-service 的泳道，bot 不会静默失效。但**泳道上下文在写入数据库后就断了**：入站没有按泳道划分的队列，之后谁读到这条消息只取决于谁在查这个库。
- **出站**：泳道队列的消息 10s TTL 到期后转回 prod 队列。泳道没有 lark-outbound 时，回复会在 10 秒后由 prod 的 lark-outbound 发出。

**回退机制不区分你想验证什么，这是最容易出现的误判。** 改动在 lark-outbound 里却没有部署它，回复由 prod 的 lark-outbound 发出：飞书里赤尾正常回复了，但你的改动一行都没有运行。改了 lark-service 的入站逻辑却没部署 lark-service 也一样，回退到 prod 后运行的是线上代码。所以**改了哪个服务就必须部署哪个服务**。

入站回退的两个边界：

- 只有**泳道的 Service 不存在**时才回退到 prod，泳道不健康时**不会**回退。lite-registry 只 watch Service、不看 ready endpoints，所以泳道 Service 还在但 Pod 没有就绪（部署中、崩溃、OOM）时，sidecar 照常转发并拿到 502，**这条消息就丢失了**（这次请求不重试）。
- lite-registry 到 sidecar 有最长 30s 的轮询延迟，刚 undeploy 或刚部署的泳道，在这段时间里 sidecar 用的还是旧的路由数据。

## 排查交接的去向

对投递方来说，「送达泳道」和「回退到 prod」都返回 200，只能从 prod 的 lark-service 日志区分（`make logs APP=lark-service`）：

- 送达泳道：`[lark-handoff] lane=<lane> took it, ...`
- 回退到 prod：`[lark-handoff] handoff for lane=<lane> was handled by lane=prod instead: ...`

指标 `lane_handoff_total{channel,target_lane,outcome}`，outcome 取值为 `lane` / `fallback` / `error`，飞书和 QQ 共用这个指标名（日志 tag 不同：QQ 侧是 `[lane-handoff]`，飞书侧是 `[lark-handoff]`）。

容易出错的细节：

- 按会话绑定（`TYPE=chat`）时，`route_key` 是 `common_conversation_id`，不是飞书的 `oc_xxx` chat_id（`packages/ts-shared/src/lane-binding/resolver.ts`）。真实群聊的 id 从 `lark_base_chat_info` 查。
- 绑定解析有 30s 的进程内缓存（`resolver.ts` 的 `CACHE_TTL_MS`）。只有 channel-server 修改绑定时会清缓存，lark-service 要等缓存过期，绑定后 30 秒内的消息可能仍按旧路由处理。
- 泳道接收端会重新生成 `common_message_id`：prod 判定需要交接后不写入这条消息，交接请求体里也不带 id。prod 日志里的 `common_message_id` 和泳道那边的不一致，排查时不能用它关联两边的记录。
- 按泳道查日志用 `make logs APP=<app> POD=<app>-<lane>`。`LANE=<非 prod>` 会被拼成 Loki 的 `lane=` selector，而 Loki 里没有 lane label，查询结果会静默为空。
- App 设置了 `AllowedLaneClasses` 时，release 只能发布到其中列出的泳道类别，其他会被 paas-engine 拒绝（`release_service.go`）。

## 泳道覆盖不到的部分

泳道部署只覆盖**交接之后**的处理路径（格式转换和写入、出站投递；agent-service 那一段只有 coe 能覆盖，见上面的泳道选择）。以下几项只在 prod 运行，泳道测不到：

- websocket 接收，以及与飞书开放平台的连接管理
- 原始报文的审计写入
- 交接之前的格式转换：prod 必须先把原始报文转换成通用格式才能查绑定，这一步运行的是 prod 的代码。（泳道收到的是原始报文，会用自己的代码再转换一遍，所以格式转换的改动在泳道上**能**验证到，但 prod 那一侧仍然是线上代码。）
- 泳道判定（LaneBindingResolver 的绑定查询和交接决策）。泳道进程收到的请求已经判定过一次，不会再判定。

原因是只有 prod 部署持有 websocket 长连接，也只有 prod 的进程会发起交接。改这些入站逻辑时，泳道验证通过后仍需在 prod 灰度观察，不能只依赖泳道测试的结论。
