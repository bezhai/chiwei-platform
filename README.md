# chiwei-platform

Monorepo 平台，包含多个微服务和共享包。部署在 K8s `prod` namespace，通过 PaaS Engine 管理构建和蓝绿部署。

## 目录结构

```
apps/
  paas-engine/      # PaaS 引擎 (Go) - 管理应用构建和蓝绿部署
  lite-registry/    # 泳道注册表 (Go) - 提供泳道路由数据
  lark-server/      # 飞书机器人服务 (TypeScript)
  lark-proxy/       # 飞书事件代理 (TypeScript)
  agent-service/    # AI Agent 服务 (Python)
  tool-service/     # 工具服务 (Python)
packages/
  ts-shared/        # TypeScript 共享工具
  py-shared/        # Python 共享工具
  lark-utils/       # 飞书 SDK 封装
  pixiv-client/     # Pixiv API 客户端
docs/
  build-convention.md  # 构建约定与服务接入指南
```

## 文档

- [构建约定与服务接入指南](docs/build-convention.md) — 构建模式、Dockerfile 编写、新服务接入步骤
- [泳道路由架构](docs/archive/lane-routing.md) — Lite-Registry + LaneRouter SDK 架构存档

## 快速开始

### PaaS Engine 开发

```bash
cd apps/paas-engine
make build    # 编译
make test     # 测试
make lint     # go vet
```

### 部署

```bash
# 普通服务一键部署
make deploy APP=my-service

# paas-engine 蓝绿自部署
make self-deploy
```

详见 [CLAUDE.md](CLAUDE.md) 中的部署命令说明。
