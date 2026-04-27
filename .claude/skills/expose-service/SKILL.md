---
name: expose-service
description: 让服务的 API 能从集群外部通过 $PAAS_API 访问。当需要添加新的外部可达路由、排查"从外面访问不到 API"问题、或有人试图使用 Ingress/LoadBalancer/NodePort/port-forward 时触发此 skill。本项目没有 Ingress Controller，外部访问的唯一通路是 api-gateway 反向代理。
user_invocable: true
---

# Expose Service

## 架构（禁止违反）

```
外部请求 → $PAAS_API (api-gateway:30080) → lite-registry 查服务 → 目标 ClusterIP Service
```

**不存在其他外部访问方式。** 以下全部不通，禁止尝试：

- Ingress 资源
- Service type: LoadBalancer / NodePort
- kubectl port-forward
- 直连 Pod IP / svc.cluster.local / localhost:端口

## 添加外部路由

### 1. 编辑 routes.yaml

文件：`apps/api-gateway/config/routes.yaml`

```yaml
  - prefix: /api/<service-name>/
    service: <service-name>
    port: <服务端口>
    # strip_prefix: /api/<service-name>  # 可选：转发时去掉前缀
```

字段说明：
- `prefix`: 路径前缀，必须以 `/` 结尾。最长前缀优先匹配
- `service`: K8s Service 名称（= PaaS Engine 中的 app name）
- `port`: 服务监听端口
- `strip_prefix`: 可选。如果服务路由从 `/` 开始而前缀是 `/api/foo`，加此字段

### 2. 重建部署 api-gateway

routes.yaml 是构建时打包进镜像的，不是 ConfigMap。改了必须重建：

```bash
make deploy APP=api-gateway GIT_REF=<包含改动的分支或 main>
```

### 3. 验证

```bash
.claude/skills/api-test/scripts/http.sh GET "$PAAS_API/<prefix>/health"
```

## 泳道路由（无需额外配置）

部署到泳道时 PaaS Engine 自动创建 `<APP>-<LANE>` Service，api-gateway 通过 lite-registry 自动发现。请求加 `x-lane` 即可路由：

```
$PAAS_API/api/<service>/path                   → prod
$PAAS_API/api/<service>/path?x-lane=feat-test  → feat-test 泳道
```

## 排错

| 症状 | 原因 | 解法 |
|------|------|------|
| 404 | routes.yaml 无匹配 prefix | 检查 routes.yaml + api-gateway 是否重新部署 |
| 502 | 服务未运行或端口错 | `/ops pods <APP>` 检查 pod + 核对端口 |
| 连接超时 | 没走 $PAAS_API | 所有请求必须通过 `$PAAS_API` |
