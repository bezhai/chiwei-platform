# PaaS Engine — 改进建议

## P0 — 安全基线

- [ ] HTTPS / TLS termination（通过 Ingress 或 sidecar 处理）
- [ ] 请求 ID 注入（方便日志追踪）
- [ ] 敏感配置（DB 密码、token）从 Secret 挂载，不硬编码环境变量默认值

## P1 — 可观测性

- [ ] 结构化错误码（统一 error response 格式，包含 code + message）
- [ ] Prometheus metrics（请求延迟、状态码分布、Build 队列深度）
- [ ] OpenTelemetry tracing 集成

## P2 — API 规范

- [ ] 分页（List 接口增加 `?page=&page_size=`，返回 total）
- [ ] 输入校验增强（统一使用 validator tag，返回字段级错误）
- [ ] API 版本策略文档化

## P3 — 架构演进

- [ ] Build 异步化（当前同步等待 Kaniko Job，改为事件驱动）
- [ ] 数据库 migration 工具（golang-migrate 或 atlas）
- [ ] 集成测试 + testcontainers（PostgreSQL + K8s mock）

## P4 — 运维体验

- [ ] Helm Chart / Kustomize 部署清单
- [ ] CI pipeline（lint + test + build image）
- [ ] Graceful degradation 文档（无 K8s 集群时的行为说明）
