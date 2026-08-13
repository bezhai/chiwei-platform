# 启动模块 (Startup Module)

负责应用的初始化、启动与优雅关闭，面向主服务的生命周期编排。

## 目录结构

```
startup/
├── application.ts      # 应用程序管理器（生命周期编排）
├── database.ts         # 数据库初始化/关闭
├── server.ts           # Hono HTTP 服务器管理
└── README.md
```

## 启动编排（Mermaid）

```mermaid
sequenceDiagram
  participant App as ApplicationManager
  participant DB as DatabaseManager
  participant Bot as botDirectory
  participant Runtime as ChannelRuntimes
  participant MQ as RabbitMQ
  participant HTTP as HttpServerManager

  App->>DB: initialize()
  App->>Bot: load()
  App->>Runtime: initializeChannelRuntimes()
  App->>Runtime: runChannelInitializers()
  App->>MQ: connect() + declareTopology()
  App->>MQ: startInboundLaneConsumer()（仅泳道部署）
  App->>Runtime: startDirectIngresses()（按 channel 策略）
  App-->>App: start()
  App->>HTTP: start() (各 channel runtime 注册 http ingress)
```

## 关键职责

- 初始化：数据库、bot 目录（`botDirectory`）、各 channel runtime、RabbitMQ
- 启动：各 channel 的主动入口（按策略）、HTTP 服务（由 runtime 注册 ingress）
- 关闭：处理 SIGINT/SIGTERM，优雅释放资源（DB/Redis 等）
- 可观察性：打印当前加载的机器人配置与路由列表

## 使用示例

```typescript
// src/index.ts
import { ApplicationManager, createDefaultConfig, setupProcessHandlers } from './startup/application';

(async () => {
  const config = createDefaultConfig();
  const app = new ApplicationManager(config);
  setupProcessHandlers(app);
  await app.initialize();
  await app.start();
})();
```
