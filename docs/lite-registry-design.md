# Lite-Registry：极简泳道路由方案

> 替代 Istio VirtualService，基于 K8s Service DNS + 服务名约定实现泳道路由

## 1. 架构动机

### 1.1 弃用 Istio 的原因

| 痛点 | 说明 |
|------|------|
| 黑盒排障 | Envoy sidecar 出问题时，链路不可见，排查困难 |
| CRD 维护成本 | VirtualService 配置繁琐，每次 Release 都要重算路由规则 |
| 多跳延迟 | 每次请求多经过一层 sidecar proxy |
| MQ/异步不支持 | Istio 只能路由 HTTP 流量，MQ 消费端无法按泳道隔离 |
| 资源开销 | 每个 Pod 注入 Envoy sidecar，在 K3s 小集群上浪费严重 |

### 1.2 为什么不直接上 Consul

K3s 内部已有完整的服务状态（API Server / etcd），再部署一套 Consul 集群是资源浪费。

### 1.3 规模决定架构

**实际规模：<10 个服务，<20 个 Pod。**

在这个规模下，Pod IP 直连 + 客户端负载均衡是过度设计。K8s 原生的 Service（kube-proxy round-robin）+ CoreDNS 已经完全够用。SDK 只需要知道"哪个泳道的 Service 存在"，然后做**域名拼接**即可。

### 1.4 目标架构

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

**关键差异（vs 上一版）：**
- SDK **不直连 Pod IP**，走 K8s Service DNS → kube-proxy
- Lite-Registry **只 Watch Services**，不关心 Pod/Endpoints
- 路由表从 Pod 级精简为**泳道存在性**（`service → [lane1, lane2]`）
- 负载均衡、健康检查全部交给 K8s 原生机制

---

## 2. 核心机制：基于服务名约定的 DNS 路由

paas-engine 部署 Release 时已经在创建命名规范的 Service：

| 资源 | 命名 | Selector | 说明 |
|------|------|----------|------|
| Lane Service | `{app}-{lane}` | `app=X, lane=Y` | 精确选中指定泳道 Pod |
| Base Service | `{app}` | `app=X, lane=prod` | 默认指向 prod |

SDK 的路由逻辑极其简单：
- 有 `x-env: dev` → 请求 `http://agent-service-dev:8000`
- 无 header 或泳道不存在 → 请求 `http://agent-service:8000`（fallback 到 prod）

**不需要改 paas-engine 的 Deployer，现有 Service 创建逻辑完全适配。**

---

## 3. 控制面：Lite-Registry

### 3.1 定位

极简的"点名册"服务：只告诉 SDK 当前集群里哪些服务有哪些泳道的 Service 存在。

### 3.2 部署要求

- 语言：Go
- 副本：Replicas: 2
- 资源占用：极低（只 Watch Services，内存占用 <10MB）
- 位置：`apps/lite-registry/`

### 3.3 K8s 权限

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: lite-registry
rules:
  - apiGroups: [""]
    resources: ["services"]
    verbs: ["get", "list", "watch"]
```

只需要 Service 的只读权限，比上一版精简了 Pod / Endpoints / Deployments。

### 3.4 内部逻辑

```
Watch Services (labelSelector: app)
  → 过滤出包含 label "app" 和 "lane" 的 Service
  → 按 app 分组，收集该 app 下存在的 lane 列表
  → 读取 Service 的 spec.ports[0].port 作为服务端口
  → prod 泳道的 Service 名为 {app}-prod，不是 {app}
     但 {app}（base service）始终存在，指向 prod
```

### 3.5 HTTP API

#### `GET /v1/routes`

返回所有服务的泳道存在性和端口。

**响应：**

```json
{
  "updated_at": "2026-02-28T10:00:00Z",
  "services": {
    "lark-server":   { "lanes": ["prod", "dev", "test"], "port": 3000 },
    "agent-service": { "lanes": ["prod", "dev"],         "port": 8000 },
    "tool-service":  { "lanes": ["prod"],                "port": 8000 },
    "lark-proxy":    { "lanes": ["prod"],                "port": 3003 }
  }
}
```

端口来自 K8s Service 的 `spec.ports[0].port`，SDK 无需硬编码。

#### `GET /v1/routes/{service_name}`

返回单个服务的泳道列表和端口。

```json
{
  "service": "lark-server",
  "lanes": ["prod", "dev", "test"],
  "port": 3000,
  "updated_at": "2026-02-28T10:00:00Z"
}
```

#### `GET /healthz`

健康检查。

---

## 4. 数据面：多语言 SDK

### 4.1 SDK 核心职责

1. **后台轮询**：每 30s 从 Lite-Registry 拉取泳道列表和端口（Service 变更频率极低）
2. **地址解析**：根据 `x-lane` header 拼接 `host:port`，调用方只需传服务名
3. **Header 透传**：自动将 `x-lane` 注入下游请求
4. **两级容灾**：泳道不存在 → fallback 到 base service；连不上 Registry → 用缓存

### 4.2 需要实现的 SDK

| 语言 | 服务 | 优先级 |
|------|------|--------|
| TypeScript (Bun) | lark-server, lark-proxy | P0 |
| Python | agent-service | P0 |
| Go | 预留 | P2 |

### 4.3 TypeScript SDK

```typescript
interface ServiceInfo {
    lanes: string[];
    port: number;
}

class LaneRouter {
    private services: Record<string, ServiceInfo> = {};
    private timer: Timer;

    constructor(private registryUrl: string, pollInterval = 30_000) {
        this.poll();
        this.timer = setInterval(() => this.poll(), pollInterval);
    }

    /**
     * 解析目标地址，返回 "host:port"
     * 调用方只需传服务名，端口由 Registry 提供
     */
    resolve(service: string, lane?: string): string {
        const info = this.services[service];
        if (!info) return `${service}`;  // 未知服务，fallback 到 DNS

        const host = (lane && info.lanes.includes(lane))
            ? `${service}-${lane}`
            : service;
        return `${host}:${info.port}`;
    }

    /**
     * 创建带泳道路由的 fetch 封装
     * 调用方只需传服务名，端口和泳道全自动处理
     */
    fetch(service: string, path: string, init?: RequestInit): Promise<Response> {
        const lane = context.getLane();
        const target = this.resolve(service, lane);
        const headers = new Headers(init?.headers);
        if (lane) headers.set('x-lane', lane);
        return fetch(`http://${target}${path}`, { ...init, headers });
    }

    private async poll() {
        try {
            const res = await fetch(`${this.registryUrl}/v1/routes`);
            const data = await res.json();
            this.services = data.services;
        } catch (err) {
            console.warn('Failed to poll Lite-Registry, using cached data', err);
        }
    }
}
```

**使用示例（lark-server 中调用 agent-service）：**

```typescript
const router = new LaneRouter('http://lite-registry:8080');

// 只传服务名，端口和泳道路由全自动
const res = await router.fetch('agent-service', '/chat/sse', { method: 'POST', body: ... });
```

### 4.4 Python SDK

```python
import threading
import requests
import logging

logger = logging.getLogger(__name__)

class LaneRouter:
    def __init__(self, registry_url: str, poll_interval: int = 30):
        self._registry_url = registry_url
        self._services: dict[str, dict] = {}  # {"lanes": [...], "port": N}
        self._poll_interval = poll_interval
        self._start_polling()

    def resolve(self, service: str, lane: str | None = None) -> str:
        """解析目标地址，返回 'host:port'。调用方只需传服务名。"""
        info = self._services.get(service)
        if not info:
            return service  # 未知服务，fallback 到 DNS

        host = f"{service}-{lane}" if (lane and lane in info["lanes"]) else service
        return f"{host}:{info['port']}"

    def base_url(self, service: str, lane: str | None = None) -> str:
        """返回完整的 base URL"""
        return f"http://{self.resolve(service, lane)}"

    def _poll(self):
        while True:
            try:
                resp = requests.get(f"{self._registry_url}/v1/routes", timeout=5)
                self._services = resp.json()["services"]
            except Exception as e:
                logger.warning(f"Poll failed, using cached data: {e}")
            threading.Event().wait(self._poll_interval)

    def _start_polling(self):
        t = threading.Thread(target=self._poll, daemon=True)
        t.start()
```

**使用示例（agent-service 中回调 lark-server）：**

```python
router = LaneRouter('http://lite-registry:8080')

lane = get_lane()  # 从 contextvars 读取
url = router.base_url('lark-server', lane)
resp = await client.post(f"{url}/api/image/process", ...)
```

### 4.5 Go SDK（预留）

```go
package lanerouter

type ServiceInfo struct {
    Lanes []string
    Port  int
}

type Router struct {
    registryURL string
    mu          sync.RWMutex
    services    map[string]ServiceInfo // service → {lanes, port}
}

// Resolve 返回 "host:port"，调用方只需传服务名
func (r *Router) Resolve(service, lane string) string {
    r.mu.RLock()
    defer r.mu.RUnlock()

    info, ok := r.services[service]
    if !ok {
        return service // 未知服务，fallback 到 DNS
    }

    host := service
    if lane != "" {
        for _, l := range info.Lanes {
            if l == lane {
                host = service + "-" + lane
                break
            }
        }
    }
    return fmt.Sprintf("%s:%d", host, info.Port)
}
```

---

## 5. 容灾设计

相比上一版的三级容灾，简化为两级（因为不直连 Pod IP，不需要额外的 DNS 降级）：

```
请求到达 → SDK.getHost(service, lane)
              │
              ├─ 泳道存在 → "{service}-{lane}" (K8s Service DNS) ✅
              │
              └─ 泳道不存在 / Registry 不可达 / 缓存为空
                  → "{service}" (base service, 指向 prod) ✅
                     始终可用，不阻断业务
```

**为什么不需要第三级？**

base service（`{app}`）由 paas-engine 在首次 Release 时创建，selector 固定指向 `lane=prod`。只要 prod 泳道有 Pod 在跑，这个 DNS 永远可达。SDK 最差情况就是忽略泳道，所有流量走 prod — 这正是我们想要的降级行为。

---

## 6. Header 命名

继续使用 `x-lane`（与现有代码一致），暂不迁移到 `x-env`，避免无谓的改动。

如果后续有明确需求再考虑重命名。

---

## 7. 各业务服务适配计划

### 7.1 现状：服务调用链

```
外部 Webhook
    ↓
lark-proxy (Port 3003, TS/Bun)
  │ 查 lane_routing 表 → 注入 x-lane header
  │ 转发到 lark-server（当前硬编码域名）
  ↓
lark-server (Port 3000, TS/Bun)
  │ bot-context middleware 读取 x-lane → AsyncLocalStorage
  │ http/client.ts 自动透传 x-lane 到下游
  ├──→ agent-service (Port 8000, Python/FastAPI) - SSE
  │      │ HeaderContextMiddleware 读取 → contextvars
  │      └──→ 回调 lark-server 图片 API (透传 x-lane)
  └──→ tool-service (Port 8000, Python/FastAPI)
         无 x-lane 处理（末端服务）
```

### 7.2 适配改造清单

#### lark-proxy（TS/Bun）— 泳道入口，P0

| 文件 | 当前逻辑 | 改动 |
|------|----------|------|
| `src/forwarder.ts` | 查 DB 得到 lane，转发时注入 `x-lane` header，目标域名硬编码 | 引入 SDK，用 `router.getHost("lark-server", lane)` 拼接目标域名 |
| `src/lane-resolver.ts` | 查 PostgreSQL `lane_routing` 表 | **不变** |
| 新增 | — | 初始化 `LaneRouter` 实例 |

#### lark-server（TS/Bun）— 核心中间层，P0

| 文件 | 当前逻辑 | 改动 |
|------|----------|------|
| `src/middleware/bot-context.ts` (行 8) | 读取 `x-lane` header → 存入 AsyncLocalStorage | **不变**（header 名不改） |
| `src/infrastructure/http/client.ts` (行 17-20) | 创建 HTTP 客户端时透传 `x-lane` | 引入 SDK，用 `router.fetch(service, path)` 替代手动拼域名 |
| `src/infrastructure/integrations/tool-service/image-client.ts` (行 45-46) | 手动注入 `x-lane` 调用 tool-service | 改用 SDK 的 `router.fetch("tool-service", path)` |
| `src/core/services/ai/chat.ts` (行 36) | 向 agent-service SSE 接口注入 `x-lane` | 改用 SDK 的 `router.fetch("agent-service", path)` |

#### agent-service（Python/FastAPI）— P0

| 文件 | 当前逻辑 | 改动 |
|------|----------|------|
| `app/utils/middlewares/trace.py` (行 28-32) | 读取 `x-lane` → contextvars | **不变** |
| `app/clients/image_client.py` (行 52, 107) | 手动透传 `x-lane` 回调 lark-server | 引入 SDK，用 `router.base_url("lark-server", lane)` |
| `app/main.py` | — | 初始化 `LaneRouter` 实例 |

#### tool-service（Python/FastAPI）— P2

**无需改动。** 末端服务，不调用其他服务。只需 Pod 有正确的 `app` + `lane` 标签（paas-engine 已保证）。

#### paas-engine（Go）— P1

| 文件 | 改动 |
|------|------|
| `internal/adapter/kubernetes/virtualservice.go` | **删除整个文件**（122 行） |
| `internal/port/kubernetes.go` | 删除 `VirtualServiceReconciler` 接口 |
| `internal/service/release_service.go` | 删除 `vsReconciler` 调用（行 131-139, 189-204） |
| `cmd/paas-engine/main.go` | 删除 dynamic client 初始化（行 62-65） |
| `internal/adapter/kubernetes/deployer.go` | **不变** — Deployment/Service 照常创建 |

---

## 8. 南北向入口（Ingress）

继续使用 K3s 自带的 Traefik。

对于动态泳道选择（按 Bot/Chat 路由），由 lark-proxy 负责。Traefik 只做反向代理，不参与泳道逻辑。

---

## 9. MQ 泳道路由（后续迭代）

SDK 同样适用于 MQ 场景：

- **生产端**：发消息时注入 `x-lane` 到消息 Header
- **消费端**：消费前检查消息的 `x-lane`，不匹配则 nack/requeue

具体方案待实际需求时细化。

---

## 10. 实施路线图

### Phase 1：Lite-Registry 落地

- [x] 创建 `apps/lite-registry/` 项目
- [x] 实现 K8s Watch Services（按 `app` + `lane` label 过滤）
- [x] 实现内存聚合：`service → [lane1, lane2, ...]`
- [x] 实现 `GET /v1/routes` 和 `GET /v1/routes/{service}` API
- [x] 实现 `GET /healthz`
- [x] 单元测试
- [x] Dockerfile + 部署

### Phase 2：SDK 开发

- [ ] TypeScript SDK（`LaneRouter` 类）
  - 轮询 + 本地缓存
  - `getHost()` 域名拼接
  - `createFetch()` 封装
- [ ] Python SDK（`LaneRouter` 类）
  - 同上

### Phase 3：业务服务适配

- [ ] lark-proxy 接入 TS SDK
- [ ] lark-server 接入 TS SDK
- [ ] agent-service 接入 Python SDK
- [ ] 全链路 dev 泳道验证

### Phase 4：Istio 剥离

- [ ] paas-engine 删除 VirtualService 相关代码
- [ ] 卸载 Istio sidecar 注入
- [ ] 清理 Istio CRD 资源

---

## 11. 与现有系统的关系

```
paas-engine（保持）
  ├── Build：Kaniko 构建镜像 → 不变
  ├── Release：部署 Deployment + Service → 不变
  │   ├── Lane Service: {app}-{lane} → SDK 路由目标
  │   └── Base Service: {app} → fallback 目标
  ├── Lane CRUD：泳道管理 → 不变
  └── VirtualService：❌ 删除

Lite-Registry（新增）
  └── Watch Services → 返回 service → [lanes] 映射

SDK（新增）
  ├── 轮询 Lite-Registry
  ├── 域名拼接（不直连 Pod IP）
  └── Header 透传

lark-proxy.lane_routing 表（保持）
  └── 决定哪个 Bot/Chat 走哪个泳道 → 不变
```

---

## 附录 A：现有 K8s 资源结构

paas-engine 已经创建了正确的资源，Lite-Registry 和 SDK 直接复用：

```yaml
# Deployment (由 paas-engine 创建)
metadata:
  name: lark-server-dev          # {app}-{lane}
  labels:
    app: lark-server
    lane: dev
spec:
  template:
    metadata:
      labels:
        app: lark-server
        lane: dev

# Lane Service (由 paas-engine 创建)
metadata:
  name: lark-server-dev          # SDK 路由到这里
  labels:
    app: lark-server
spec:
  selector:
    app: lark-server
    lane: dev                    # 精确选中 dev Pod

# Base Service (由 paas-engine 创建)
metadata:
  name: lark-server              # SDK fallback 到这里
spec:
  selector:
    app: lark-server
    lane: prod                   # 固定指向 prod
```

## 附录 B：各服务信息速查

| 服务 | 语言 | 端口 | 健康检查 | SDK 适配优先级 |
|------|------|------|----------|---------------|
| lark-proxy | TS/Bun | 3003 | `/api/health` | P0（泳道入口） |
| lark-server | TS/Bun | 3000 | `/api/health` | P0（核心中间层） |
| agent-service | Python/FastAPI | 8000 | `/api/health` | P0（回调上游） |
| tool-service | Python/FastAPI | 8000 | `/health` | P2（无需 SDK） |
| paas-engine | Go | 8080 | `/healthz` | P1（仅删 Istio） |

## 附录 C：被删除的设计（v1 → v2 变更记录）

| v1 设计 | v2 决策 | 原因 |
|---------|---------|------|
| SDK 直连 Pod IP | 走 K8s Service DNS | <20 Pod 规模下，kube-proxy 足够，省去客户端负载均衡 |
| Watch Pod + Endpoints + Deployment | 只 Watch Services | 不需要 Pod 级信息，Service 变更频率极低 |
| 路由表含 IP/端口/副本数/版本 | 只含泳道存在性 | SDK 不做负载均衡，只做域名拼接 |
| 三级容灾 | 两级容灾 | base service 始终可达，不需要 DNS 降级兜底 |
| `x-lane` → `x-env` 迁移 | 保持 `x-lane` | 避免无谓改动，现有代码已统一 |
| 轮询间隔 10s | 30s | Service 创建/删除远低于 Pod 重启频率 |
