# Sidecar 泳道路由设计

## 背景

当前泳道系统对业务代码侵入过强。每个服务需要：引入 LaneRouter SDK、手动注入 `x-lane` header、MQ 消息体塞 lane 字段。改 dashboard/monitor 等纯业务功能时也被迫处理泳道逻辑。

本设计用 sidecar 模式重构 HTTP 层泳道路由，目标：**业务代码零感知泳道**。

## 架构：两层分离

### 第一层：通用上下文传播中间件（框架级）

自动透传 `x-ctx-*` 前缀 header，lane 只是其中一个 case。

**Header 规范**：
- 前缀：`x-ctx-`
- 泳道：`x-ctx-lane`
- 可扩展：`x-ctx-trace-id`、`x-ctx-gray-group` 等，不需要改业务代码

**TS 侧**（`packages/ts-shared`）：
- Hono 中间件：入站提取所有 `x-ctx-*` header → 存入 AsyncLocalStorage context（扩展 BaseRequestContext）
- 出站 hook：从 AsyncLocalStorage 自动读出 `x-ctx-*` 附到出站请求

**Python 侧**（`packages/py-shared`）：
- 复用现有 `create_header_context_middleware()` 配置化映射，加入 `x-ctx-*`
- httpx client hook 从 contextvars 读出全量 `x-ctx-*` 附上

**Go 侧**（`apps/paas-engine`）：
- HTTP middleware：入站提取 `x-ctx-*` → 存入 `context.Context`
- 出站 HTTP client：从 `context.Context` 读出 `x-ctx-*` 自动附上

### 第二层：Go Sidecar（基础设施级）

透明拦截 Pod 内出站 HTTP，根据 `x-ctx-lane` header 路由到对应泳道实例。

**技术选型**：Go，`net/http/httputil.ReverseProxy`，轻量单二进制。

**核心流程**：
```
业务容器发出 HTTP 请求
  → iptables 重定向到 sidecar (localhost:15001)
  → sidecar 解析 original destination (SO_ORIGINAL_DST)
  → 判断是否集群内服务（目标是 K8s Service ClusterIP）
  → 是：读 x-ctx-lane header
       → 有泳道实例 → 转发到 {service}-{lane}:{port}
       → 无泳道实例 → 转发到 {service}:{port}（prod fallback）
  → 否（外部流量）：直接转发，不干预
```

**路由数据来源**：
- 启动时从 lite-registry 拉 `/v1/routes`，后台定期轮询
- 本地缓存，lite-registry 不可达时用缓存兜底

**不拦截的流量**：
- sidecar 自身的出站（UID 1337 排除，避免死循环）
- localhost 回环流量
- 健康检查探针

**端口**：
- `15001` — 出站流量拦截
- `15021` — sidecar 自身健康检查

**可观测性**：
- Prometheus metrics：请求数、延迟、路由命中/fallback 计数
- 请求日志（可配置开关）

## iptables Init Container

轻量 init container，设置 iptables 规则：

```bash
# sidecar 进程自身不拦截（避免死循环）
-A OUTPUT -m owner --uid-owner 1337 -j RETURN
# localhost 不拦截
-A OUTPUT -d 127.0.0.1/32 -j RETURN
# 其余出站 TCP 重定向到 sidecar
-A OUTPUT -p tcp -j REDIRECT --to-port 15001
```

需要 `NET_ADMIN` capability。

## PaaS Engine 注入

- 应用级开关：`sidecar: true`
- 开启后 PaaS Engine 生成 Deployment spec 时自动：
  1. 加 init container（iptables 规则）
  2. 加 sidecar container（Go proxy，共享网络 namespace）
  3. 设 sidecar container `runAsUser: 1337`
  4. 注入 `LANE` 环境变量（Pod 所属泳道）
  5. 注入 `REGISTRY_URL` 环境变量

## 迁移路径

### Step 1：基础设施就绪
- 开发 Go sidecar + init container 镜像
- PaaS Engine 支持 `sidecar: true` 注入
- TS/Python/Go 三端实现 `x-ctx-*` 通用上下文传播中间件
- 不影响现有服务

### Step 2：灰度接入
- `monitor-dashboard` 作为第一个试点
- 开启 `sidecar: true`，部署到测试泳道验证
- 验证通过后删除手工 URL 改写逻辑
- 逐步推到 `lark-server`、`agent-service`

### Step 3：收尾清理
- 删除 LaneRouter SDK 中的路由逻辑
- `x-lane` 迁移为 `x-ctx-lane`
- LaneRouter 降级或删除

**向前兼容**：Step 2 期间 sidecar 和 LaneRouter 共存无冲突——sidecar 在网络层路由，LaneRouter 多注入的 header 不影响。

## MQ 层

本设计不涉及 MQ 层的泳道路由。MQ 的 lane 传播（队列名拼接、消息体 lane 字段）保持现状，后续单独处理。

## 不在范围内

- Istio / Envoy：过重，本项目只需泳道路由，不需要完整 service mesh
- MQ sidecar：sidecar 不适合处理 MQ 的队列名拼接逻辑
- DNS 劫持 / HTTP_PROXY 方案：iptables 更可靠，且 PaaS Engine 完全掌控部署
