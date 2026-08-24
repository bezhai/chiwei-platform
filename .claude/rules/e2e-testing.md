# 飞书 Dev 泳道端到端测试

## 核心原则

飞书相关链路测试必须绑定 dev bot 到目标泳道。只需要部署改动的服务，不需要全部部署——未部署的服务会 fallback 到 prod。

## 泳道选择

飞书 dev bot 测试可走 `ppe-<name>` 或 `coe-<name>`，由你的改动会写什么决定：

- **`ppe-<name>`（共用 prod 组件）**：表 / 历史 / 种子配置都用线上的，开箱即用。代价：dev bot 触发的所有写入（消息记录、recall、新表新字段）直接落 prod，schema 变更或脏数据会污染线上历史。**适合**：纯读路径、prompt 调优、不动 DB 的逻辑。
- **`coe-<name>`（独立 chiwei-test 容器）**：写入只影响 chiwei-test，破坏不外溢。代价是要**提前准备 chiwei-test 数据**：
  - **schema**：`ensure_business_schema()` 在 coe-* 启动时自动建，但只覆盖 framework 注册过的 Data；新加的表 / 字段没注册就建不出来，要先在 framework 里注册
  - **种子数据**：dev bot 跑通必须读到的 user / persona / bot 配置等，要从 prod dump 一份到 chiwei-test 对应库
  - **适合**：schema 变更、消息协议变更、写量爆炸 / 写脏风险的改动

## 标准流程

1. 部署改动的服务到独立泳道：`make deploy APP=<app> LANE=<lane>`（`<lane>` 按上方规则选 `ppe-<name>` 或 `coe-<name>`）。lark-service 会自动同步 release lark-outbound 到同一泳道。
2. 如果走 coe：确认 schema 已建 + 必要种子数据已复刻到 chiwei-test
3. 绑定 dev bot：`/ops bind TYPE=bot KEY=dev LANE=<lane>`
4. 在飞书 dev bot 发消息验证
5. 验证完毕后清理：
   - `/ops unbind TYPE=bot KEY=dev`
   - `make undeploy APP=<app> LANE=<lane>`

## 消息流转链路

飞书入站走 lark-service websocket 长连，**只有 prod 部署持连**（`LARK_DIRECT_INGRESS=true`）。泳道部署不连 websocket，靠泳道信封收消息：

```
飞书 --websocket--> lark-service(prod)
  → LaneBindingResolver 查 lane_routing 表（chat 优先，bot 其次）
  → 命中泳道：封装信封投递到 inbound_lane.lark.<lane> 队列
  → lark-service(<lane>) 消费信封（不做审计落库，按 channel+事件+消息+lane 去重）
  → agent-service(<lane>)（LaneRouter 按 context lane 路由）
  → chat_response_lark_<lane> 队列
  → lark-outbound(<lane>) → 飞书回复
```

未绑定泳道的消息（含未绑定时的 dev bot）走 prod 全链路。

## 泳道测不到的部分

泳道部署只覆盖**信封消费之后**的处理路径（事件处理、agent 调用、出站）。以下逻辑只在 prod 跑，泳道测不到：

- websocket 接收与飞书开放平台的连接管理
- 事件投影（原始报文 → 通用口径）
- 泳道判定（LaneBindingResolver 的绑定查询与信封投递决策）

改动这些入站逻辑时，泳道验证通过后仍需在 prod 灰度观察，不能只依赖泳道测试结论。
