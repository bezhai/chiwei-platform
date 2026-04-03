# 统一 HTTP 框架到 Hono

## 背景

当前 monorepo 中三个 TS 服务使用不同的 HTTP 框架：

| 服务 | 运行时 | 框架 | 路由数 | ctx 渗透深度 |
|------|--------|------|--------|-------------|
| lark-server | Bun | Koa | 3 | 浅 (~23处) |
| lark-proxy | Bun | Hono | 7 | 浅 (~20处) |
| monitor-dashboard | Node | Koa | 37 | 深 (~155处, 14文件) |

`@inner/shared` 包中有 4 个 Koa 专属中间件（auth、error-handler、trace、validation），lark-proxy 无法复用。

## 决策

- **目标框架：Hono**
- **monitor-dashboard 运行时：保持 Node**（通过 `@hono/node-server` 适配）
- **迁移策略：逐服务**（lark-server → monitor-dashboard）
- **shared 中间件：直接重写为 Hono 版本**

## 迁移范围

### Phase 1：`@inner/shared` 中间件重写

将 4 个 Koa 中间件重写为 Hono 版本：

- `middleware/auth.ts` — Bearer 认证，`ctx.state` → `c.set()`
- `middleware/error-handler.ts` — 统一错误处理，`ctx.status`/`ctx.body` → `c.json()`
- `middleware/trace.ts` — TraceId 注入，`ctx.request.headers`/`ctx.set()` → `c.req.header()`/`c.header()`
- `middleware/validation.ts` — 请求校验，`ctx.request` → `c.req`

同时：
- 删除 `koa` peer dependency，改为 `hono`
- 更新类型声明

### Phase 2：lark-server 迁移（3 路由）

- 入口 `new Koa()` → `new Hono()` + `export default { port, fetch }`（Bun 原生模式）
- 3 个路由从 Koa Router 改为 Hono 路由
- 7 个中间件从 Koa 模式改为 Hono 模式
- 移除依赖：koa、@koa/router、@koa/cors、koa-body 及其类型包
- 验证：health、metrics、lark-event 三个端点正常

### Phase 3：monitor-dashboard 迁移（37 路由）

- 安装 `@hono/node-server`
- 入口从 `new Koa()` + `app.listen()` → `new Hono()` + `serve({ fetch, port })`
- 37 个路由处理函数：`ctx.*` → `c.*`
- 155 处 ctx 引用逐一替换，主要映射：
  - `ctx.body = x` → `return c.json(x)`
  - `ctx.status = 404` → `return c.json(body, 404)`
  - `ctx.params.id` → `c.req.param('id')`
  - `ctx.query.key` → `c.req.query('key')`
  - `ctx.request.body` → `await c.req.json()`
  - `ctx.state.user` → `c.get('user')`
  - `ctx.get('header')` → `c.req.header('header')`
- 中间件迁移：cors、bodyParser、errorHandler、jwtAuth、auditMiddleware
- 移除依赖：koa、@koa/router、@koa/cors、koa-bodyparser 及其类型包
- 运行时保持 Node

### Phase 4：清理

- 删除 `@inner/shared` 中残留的 Koa 类型和旧代码
- 确认 monorepo 的 pnpm-lock 中零 Koa 依赖
- 更新 CLAUDE.md 中关于框架的描述（如有）

## 风险与应对

| 风险 | 应对 |
|------|------|
| monitor-dashboard `ctx.state` 中间件间传递 auth 信息 | Hono 用 `c.set()`/`c.get()` + Variables 类型声明替代 |
| Phase 1 改 shared 后 monitor-dashboard 暂时无法用 shared 中间件 | Phase 3 之前 monitor-dashboard 临时保留自己的 Koa 中间件 |
| 37 路由逐个改写工作量大 | 机械替换，无技术风险，可并行处理多个路由文件 |

## 不做的事

- 不迁移 monitor-dashboard 运行时（保持 Node）
- 不重构业务逻辑，只替换框架胶水层
- 不改 lark-proxy（已是 Hono）
