# 飞书 Dev 泳道端到端测试

## 核心原则

飞书相关链路测试必须绑定 dev bot 到目标泳道。**改哪个服务就部哪个服务，别的服务不用陪部**——没部署的那些，流量会自动落回 prod 实例（入站靠 lane-sidecar，出站靠队列 TTL，见下文）。反过来说，你想验的改动在哪个服务里，那个服务就必须部到泳道，否则跑的是 prod 的线上代码而你看不出来。

## 泳道选择

飞书 dev bot 测试可走 `ppe-<name>` 或 `coe-<name>`，由你的改动会写什么决定：

- **`ppe-<name>`（共用 prod 组件）**：表 / 历史 / 种子配置都用线上的，开箱即用。代价：dev bot 触发的所有写入（消息记录、recall、新表新字段）直接落 prod，schema 变更或脏数据会污染线上历史。**适合**：纯读路径、prompt 调优、不动 DB 的逻辑。
- **`coe-<name>`（独立 chiwei-test 容器）**：写入只影响 chiwei-test，破坏不外溢。代价是要**提前准备 chiwei-test 数据**：
  - **schema**：`ensure_business_schema()` 在 coe-* 启动时自动建，但只覆盖 framework 注册过的 Data；新加的表 / 字段没注册就建不出来，要先在 framework 里注册
  - **种子数据**：dev bot 跑通必须读到的 user / persona / bot 配置等，要从 prod dump 一份到 chiwei-test 对应库
  - **适合**：schema 变更、消息协议变更、写量爆炸 / 写脏风险的改动

## 标准流程

1. 部署改动的服务到独立泳道：`make deploy APP=<app> LANE=<lane>`（`<lane>` 按上方规则选 `ppe-<name>` 或 `coe-<name>`）。lark-service 和 lark-outbound 是同一镜像的两个 Deployment，部署 lark-service 会自动同步 release lark-outbound 到同一泳道，所以改这两个中的任何一个都用 `APP=lark-service`。
2. 如果走 coe：确认 schema 已建 + 必要种子数据已复刻到 chiwei-test
3. 绑定 dev bot：`/ops bind TYPE=bot KEY=dev LANE=<lane>`
4. 在飞书 dev bot 发消息验证
5. 验证完毕后清理：
   - `/ops unbind TYPE=bot KEY=dev`
   - `make undeploy APP=<app> LANE=<lane>`

## 消息流转链路

飞书入站走 lark-service websocket 长连，**只有 prod 部署持连**（闸是 `isProdDeployment() && LARK_DIRECT_INGRESS === 'true'`）。泳道部署不连 websocket，消息由 prod 通过一次内部 HTTP 交接过来：

```
飞书 --websocket--> lark-service(prod)
  → 投影成 common 口径，LaneBindingResolver 查 lane_routing 表（chat 优先，bot 其次）
  → 命中泳道 X：带 x-ctx-lane: X 打一次 POST /api/internal/lark/lane-inbound
      （QQ 侧同构，路径是 /api/internal/qq/lane-inbound）
  → lane-sidecar 透明选路：读 header 查 lite-registry，把 lark-service:3000 改写成 lark-service-X:3000
  → lark-service(X) 接住信封，以信封里的 lane 建上下文继续处理
      （原始报文在 prod 那次已经记过，这里不重复审计落库）
  → chat_request_X 队列 → agent-service(X)
  → chat_response_lark_X 队列 → lark-outbound(X) → 飞书回复
```

交接打的目标服务名就是 `lark-service` 自己，泳道后缀由 sidecar 按 `x-ctx-lane` 改写，业务代码里没有任何路由逻辑。这一跳**不重试**——飞书早已应答，重试就是同一条消息处理两遍。

出站没有换成 HTTP，仍然走 MQ：`chat_response_lark_{lane}` 本来就带 10s TTL + DLX 回 prod。

未绑定泳道的消息（含未绑定时的 dev bot）走 prod 全链路。

## 没部署的服务怎么落回 prod

入站和出站各有一条兜底，这就是「只部你改的那个服务」能成立的原因：

- **入站**：泳道的 K8s Service 不存在时，sidecar 把交接请求原样打回 prod。消息由 prod 的代码处理，但**保持泳道的 lane 上下文**——`chat_request_{lane}` 照常投出去，下游泳道服务照常消费。所以绑定指向一条没部署 lark-service 的泳道不会让 bot 静默变砖。
- **出站**：泳道队列的 10s TTL 到期后消息降级回 prod 队列，泳道没有 lark-outbound 时，回复 10 秒后由 prod 的 lark-outbound 发出。

**兜底不管你想验什么，这是最容易吃的假绿。** 改动在 lark-outbound 里却没部它，回复由 prod 的 lark-outbound 发出——你在飞书看到赤尾正常回话，而你的改动一行都没跑。改 lark-service 入站逻辑不部 lark-service 同理，落回 prod 跑的是线上代码。所以：**改哪个服务就必须部哪个服务**。

两条入站兜底的边界要知道：

- 落回 prod 只在**泳道的 Service 不存在**时发生，**不是**「泳道不健康」时。lite-registry 只 watch Service、不看 ready endpoints，所以泳道 Service 还在但 Pod 没起来（部署中 / 崩溃 / OOM）时 sidecar 照转不误，拿到 502，**那条消息就丢了**（这一跳不重试）。
- lite-registry 到 sidecar 有最长 30s 的轮询延迟，刚 undeploy 或刚部署的泳道在这个窗口里 sidecar 的判断还是旧的。

## 交接落到哪儿：排查判据

投递方眼里「送达泳道」和「落回 prod」都是 200，只能从 prod 的 lark-service 日志区分（`make logs APP=lark-service`）：

- 送达泳道：`[lark-handoff] lane=<lane> took it, ...`
- 落回 prod：`[lark-handoff] handoff for lane=<lane> was handled by lane=prod instead: ...`

指标 `lane_handoff_total{channel,target_lane,outcome}`，outcome ∈ `lane` / `fallback` / `error`，飞书和 QQ 共用同一个指标名（QQ 侧的日志 tag 是 `[lane-handoff]`，飞书侧是 `[lark-handoff]`）。

## 泳道测不到的部分

泳道部署只覆盖**交接之后**的处理路径（事件处理、agent 调用、出站）。以下几件只在 prod 跑，泳道测不到：

- websocket 接收与飞书开放平台的连接管理
- 原始报文审计落库
- 交接前那一段投影：prod 必须先把原始报文投影成 common 口径才能查绑定，跑的是 prod 的代码。（泳道拿到的是原始报文，会用自己那份代码再投影一遍，所以投影改动在泳道**能**验到——但 prod 侧那一遍仍然是线上代码。）
- 泳道判定（LaneBindingResolver 的绑定查询与交接决策）。泳道进程收到的信封已经判过一次，不会再判。

根因是只有 prod 部署持 websocket 长连，且只有 prod 角色的进程会发起交接。改动这些入站逻辑时，泳道验证通过后仍需在 prod 灰度观察，不能只依赖泳道测试结论。
