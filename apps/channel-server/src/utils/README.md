# Utils

这里现在只剩一个文件：`rate-limiting/rate-limiter.test.ts`。

限流器本体不在本包 —— `RateLimiter` 由 `@inner/shared` 导出
（`packages/ts-shared/src/utils/rate-limiter.ts`），这里的用例是拿真实计时器跑的
一份行为验证。

原先住在这个目录的其它通用工具已经不在本包了：

- 状态机、文本处理（`StateMachine` / `TextUtils`）迁进了 `@inner/shared`；
- 依赖飞书的那部分（消息构造、卡片、分词词云等）随飞书渠道一起迁去了
  `apps/lark-service`；
- 只服务于飞书那条链路的（SSE 聊天客户端、聊天状态机）已随之删除。

新增通用工具优先放 `packages/ts-shared`：channel-server 与 lark-service 是两个包，
放这里另一个服务用不上。
