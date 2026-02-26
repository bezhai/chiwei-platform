# Lark 服务模块

本模块负责所有与飞书 (Lark) 平台的交互, 包含事件处理、API 调用和连接管理。

## 模块结构

```
services/lark/
├── router.ts              # HTTP 模式下的路由管理器
├── websocket.ts           # WebSocket 模式下的连接管理器
├── startup-strategy.ts    # 启动策略 (HTTP vs WebSocket)
├── events/                # 事件处理器
├── basic/                 # 基础 API 封装 (消息、群组、卡片等)
└── README.md              # 本文档
```

## 核心组件

### `HttpRouterManager`

在 HTTP 模式下, `HttpRouterManager` 负责为每个机器人创建和注册事件和卡片回调路由。

### `WebSocketManager`

在 WebSocket 模式下, `WebSocketManager` 负责为每个机器人创建和管理 WebSocket 连接, 并分发事件。

### `StartupStrategyManager`

`StartupStrategyManager` 使用策略模式, 根据配置 (`http` 或 `websocket`) 选择合适的启动方式。

### `events/`

此目录包含了所有飞书事件的具体处理逻辑。详见 [events/README.md](./events/README.md)。

### `basic/`

此目录封装了对飞书开放平台基础 API 的调用, 例如:
-   `message.ts`: 发送和更新消息
-   `group.ts`: 获取群组信息和成员列表
-   `card-manager.ts`: 构建和更新交互式卡片

## 使用方法

本模块由 `startup/application.ts` 在应用启动时自动初始化。`StartupStrategyManager` 会选择相应的策略来启动机器人实例
