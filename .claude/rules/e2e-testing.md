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

1. 部署改动的服务到独立泳道：`make deploy APP=<app> LANE=<lane>`（`<lane>` 按上方规则选 `ppe-<name>` 或 `coe-<name>`）
2. 如果走 coe：确认 schema 已建 + 必要种子数据已复刻到 chiwei-test
3. 绑定 dev bot：`/ops bind TYPE=bot KEY=dev LANE=<lane>`
4. 在飞书 dev bot 发消息验证
5. 验证完毕后清理：
   - `/ops unbind TYPE=bot KEY=dev`
   - `make undeploy APP=<app> LANE=<lane>`

## 消息流转链路

```
飞书消息
  → channel-proxy-prod  (/webhook/{bot}/event, lane_routing 查询)
  → channel-server-{lane}  (/api/internal/lark-event, x-lane 注入 context)
  → agent-service-{lane} (POST /chat/sse, LaneRouter 根据 context lane 路由)
  → chat_response_{lane} 队列
  → chat-response-worker → 飞书回复
```

## channel-proxy 自身测试（特殊流程）

channel-proxy 是飞书 webhook 入口，无法通过泳道路由测试自身。**仅 channel-proxy 允许使用临时 Ingress 劫持流量测试，其他服务禁止。**

步骤：
1. 部署到独立泳道
2. 创建临时 Ingress（priority: 100）劫持 `/webhook/` 到测试泳道
3. 飞书发消息验证
4. **立即删除临时 Ingress**
5. 下掉测试泳道

**风险**：劫持期间所有飞书 webhook 都走测试泳道，务必快速验证后立即切回。
