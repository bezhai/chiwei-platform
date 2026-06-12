# chiwei-platform

让虚拟人「赤尾（三姐妹）」在飞书里像真人一样聊天、并自主过自己生活的平台。围绕这个核心业务，仓库里还长出了支撑它的自建底座：PaaS（构建 / 部署 / 配置）、运维后台、泳道路由基础设施。整体跑在 K8s `prod` namespace。

## 目录结构

```
apps/
  agent-service/         # AI 对话 + world/life 自主生活引擎 (Python)：自研 agent 工具循环 + dataflow runtime
  alert-webhook/         # Prometheus 告警转飞书 (Go)
  api-gateway/           # 外部流量统一入口，按规则分流并注入泳道 header (Go)
  channel-server/        # 消息渠道服务：webhook 入口 + 平台无关 core + Lark 插件 (Bun/TS)
  lane-sidecar/          # 注入到每个业务 Pod 的透明代理容器，改写出站服务名做泳道路由 (Go)；非独立部署
  lite-registry/         # 泳道注册表：watch K8s Services，提供泳道路由真值表 (Go)
  media-sync-worker/     # Pixiv / Bangumi 媒体素材定时同步 (Bun/TS)
  monitor-dashboard/     # 运维后台 BFF + 审计落库 (Bun/TS)
  monitor-dashboard-web/ # 运维后台前端 SPA (React)
  paas-engine/           # 自建 PaaS：Kaniko 构建、部署、网关规则、配置管理 (Go)
  sandbox-worker/        # 隔离环境执行 bash / 技能脚本，agent 的工具后端 (Python)
  tagger-service/        # Pixiv 图片 GPU 打标管线 (Python)；裸机 systemd 部署，不进 PaaS/K8s
  tool-service/          # 图像管道 + 关键词提取，agent 的工具后端 (Python)
packages/
  lark-utils/            # 飞书 SDK 封装 (TS)
  pixiv-client/          # Pixiv API 客户端 (TS)
  py-shared/             # Python 共享基建 + LaneRouter SDK + 动态配置 SDK
  ts-shared/             # TS 共享基建（中间件 / 缓存 / 日志 / HTTP / LaneRouter SDK）
```

一个镜像可以产出多个独立的 K8s Deployment（如 channel-server 一镜像出 channel-server / recall-worker / chat-response-worker 三个服务），查日志和排查必须按实际服务名来；映射表以 [CLAUDE.md](CLAUDE.md) 为单一来源。

## 文档

- [MANIFESTO.md](MANIFESTO.md) — 赤尾宣言，项目宪法，未经许可禁止修改
- [CLAUDE.md](CLAUDE.md) — 开发流程、部署铁律、泳道命名规范、运维命令
- [docs/service-topology.md](docs/service-topology.md) — 全部服务的拓扑现状：五个面、核心数据流、队列地图、存储归属
- [docs/chiwei-system-design.md](docs/chiwei-system-design.md) — 赤尾系统设计（对话 / 世界 / 生活 / 记忆）
- [docs/build-convention.md](docs/build-convention.md) — 构建约定与新服务接入步骤
- [docs/config-management.md](docs/config-management.md) — ConfigBundle、环境变量、Dynamic Config 与最终生效优先级
- [docs/ci-pipeline-roadmap.md](docs/ci-pipeline-roadmap.md) — CI 流水线规划
- `docs/guides/` 是 dataflow 框架文档，`docs/runbooks/` 是操作手册，`docs/archive/` 是历史存档（含旧版泳道路由设计）

## 快速开始

```bash
# 部署到泳道验证（GIT_REF 必须显式写，禁止省略；分支需先 git push 到远端）
make deploy APP=channel-server LANE=ppe-demo GIT_REF=my-branch

# 部署 prod（必须先过泳道验证；LANE 默认 prod，GIT_REF 只允许 main）
make deploy APP=channel-server GIT_REF=main

# 查看状态 / 日志（Loki）
make status
make logs APP=agent-service KEYWORD=error
```

本地开发按各 app 自己的工具链：Go 服务进目录用 `make build` / `make test`，TS 服务用 `bun`，Python 服务用 `uv`。完整的部署命令、泳道规范和 e2e 测试流程见 [CLAUDE.md](CLAUDE.md)。
