# 飞书 Dev 泳道端到端测试

## 核心原则

飞书相关链路测试必须绑定 dev bot 到目标泳道。只需要部署改动的服务，不需要全部部署——未部署的服务会 fallback 到 prod。

## 标准流程

1. 部署改动的服务到独立泳道：`make deploy APP=<app> LANE=<lane>`
2. 绑定 dev bot：`/ops bind TYPE=bot KEY=dev LANE=<lane>`
3. 在飞书 dev bot 发消息验证
4. 验证完毕后清理：
   - `/ops unbind TYPE=bot KEY=dev`
   - `make undeploy APP=<app> LANE=<lane>`

## 消息流转链路

```
飞书消息
  → lark-proxy-prod  (/webhook/{bot}/event, lane_routing 查询)
  → lark-server-{lane}  (/api/internal/lark-event, x-lane 注入 context)
  → agent-service-{lane} (POST /chat/sse, LaneRouter 根据 context lane 路由)
  → safety_check_{lane} 队列 → vectorize_{lane} 队列 → recall_{lane} 队列
  → chat-response-worker → lark-server → 飞书回复
```

## lark-proxy 自身测试（特殊流程）

lark-proxy 是飞书 webhook 入口，无法通过泳道路由测试自身。**仅 lark-proxy 允许使用临时 Ingress 劫持流量测试，其他服务禁止。**

步骤：
1. 部署到独立泳道
2. 创建临时 Ingress（priority: 100）劫持 `/webhook/` 到测试泳道
3. 飞书发消息验证
4. **立即删除临时 Ingress**
5. 下掉测试泳道

**风险**：劫持期间所有飞书 webhook 都走测试泳道，务必快速验证后立即切回。
