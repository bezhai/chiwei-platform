# 泳道路由架构（已完成）

> 完成时间：2026-02-27 ~ 2026-03-01

## 架构动机

弃用 Istio VirtualService，原因：Envoy sidecar 黑盒排障困难、CRD 维护成本高、多跳延迟、MQ/异步不支持、K3s 小集群资源浪费。在 <10 服务 / <20 Pod 的规模下，K8s 原生 Service + CoreDNS 完全足够。

## 最终架构

```
┌──────────────┐
│   Traefik    │  南北向入口
└──────┬───────┘
       │
       ▼
┌──────────────┐    HTTP 轮询 (30s)    ┌──────────────────┐
│  业务服务     │◄────────────────────►│  Lite-Registry    │
│  (内嵌 SDK)  │                      │  (Watch Services) │
└──────────────┘                      └──────────────────┘
       │                                       │
       │ K8s Service DNS                       │ Watch
       │ (kube-proxy 负载均衡)                   ▼
       ▼                              ┌──────────────────┐
┌──────────────┐                      │  K8s API Server   │
│  目标 Pod    │                      │  (Services only)  │
└──────────────┘                      └──────────────────┘
```

## 核心机制：Service 命名约定 + DNS 路由

paas-engine 部署 Release 时创建：

| 资源 | 命名 | Selector | 说明 |
|------|------|----------|------|
| Lane Service | `{app}-{lane}` | `app=X, lane=Y` | 精确选中指定泳道 Pod |
| Base Service | `{app}` | `app=X, lane=prod` | 默认指向 prod |

SDK 路由逻辑：有 `x-lane` header 且泳道存在 → `{app}-{lane}:port`；否则 → `{app}:port`（fallback prod）。

## 组件说明

### Lite-Registry（`apps/lite-registry/`）

极简"点名册"服务，Watch K8s Services（按 `app` + `lane` label 过滤），聚合为 `service → {lanes, port}` 映射。

- **API**：`GET /v1/routes`（全量）、`GET /v1/routes/{service}`（单个）、`GET /healthz`
- **权限**：ClusterRole 只需 Services 的 get/list/watch
- **部署**：Go，Replicas: 2，内存 <10MB

### LaneRouter SDK（`packages/`）

多语言 SDK，负责轮询 Lite-Registry、域名拼接、header 透传。

**TypeScript**（lark-server, lark-proxy）：
```typescript
const router = new LaneRouter('http://lite-registry:8080');
const res = await router.fetch('agent-service', '/chat/sse', { method: 'POST', body });
```

**Python**（agent-service）：
```python
router = LaneRouter('http://lite-registry:8080')
url = router.base_url('lark-server', lane)
```

## 两级容灾

```
请求 → SDK.resolve(service, lane)
         ├─ 泳道存在 → "{service}-{lane}" (K8s Service DNS)
         └─ 泳道不存在 / Registry 不可达 / 缓存为空
             → "{service}" (base service, 指向 prod，始终可用)
```

## 各服务速查

| 服务 | 语言 | 端口 | 健康检查 | 角色 |
|------|------|------|----------|------|
| lark-proxy | TS/Bun | 3003 | `/api/health` | 泳道入口 |
| lark-server | TS/Bun | 3000 | `/api/health` | 核心中间层 |
| agent-service | Python/FastAPI | 8000 | `/api/health` | AI Agent |
| tool-service | Python/FastAPI | 8000 | `/health` | 末端工具服务 |
| paas-engine | Go | 8080 | `/healthz` | 部署引擎 |
| lite-registry | Go | 8080 | `/healthz` | 泳道注册表 |

## 完成时间线

| 阶段 | 内容 | 完成 |
|------|------|------|
| Phase 1 | Lite-Registry 实现 + 部署 | 2026-02-27 |
| Phase 2 | LaneRouter SDK（TS + Python） | 2026-02-28 |
| Phase 3 | 业务服务适配（lark-proxy, lark-server, agent-service） | 2026-02-28 |
| Phase 4 | Istio 剥离（paas-engine 删 VirtualService 代码） | 2026-03-01 |
