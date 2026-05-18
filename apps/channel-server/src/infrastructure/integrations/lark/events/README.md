# Lark 事件处理器

基于装饰器的动态事件注册机制，负责处理来自飞书（Lark）的各类事件。

## 核心组件

### @EventHandler 装饰器

用于将类方法标记为某个（或多个）事件类型的处理器，自动完成事件类型与处理函数的关联。

```typescript
// src/services/lark/events/handlers.ts
import { EventHandler } from '../../../utils/bot/event-handler.decorator';

export class LarkEventHandlers {
  @EventHandler('im.message.receive_v1')
  async handleMessageReceive(params: any) {
    // 处理消息接收
  }
}
```

### LarkEventHandlers

集中维护所有事件处理方法（文件：src/services/lark/events/handlers.ts）。每个方法通过 @EventHandler 进行声明式注册。

### 注册与分发

- 在系统启动时，HttpServerManager 与 WebSocketManager 会扫描 LarkEventHandlers 的装饰器元数据，构建「事件类型 -> 处理函数」映射。
- 收到 Lark 事件后，根据事件类型分发到对应处理器。

## 支持事件

覆盖消息接收/撤回、卡片操作、群成员变更、机器人进退群、表情回应等。详见 LarkEventHandlers 的方法声明。

## 添加新事件处理器

1. 在 LarkEventHandlers 中新增 async 方法
2. 使用 @EventHandler('your.event.type') 进行装饰
3. 实现处理逻辑与必要的错误处理
