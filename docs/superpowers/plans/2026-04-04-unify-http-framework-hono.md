# 统一 HTTP 框架到 Hono 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 lark-server 和 monitor-dashboard 从 Koa 迁移到 Hono，统一 monorepo 中所有 TS 服务的 HTTP 框架。

**Architecture:** 分三阶段：先改 `@inner/shared` 中间件（Koa→Hono），再迁移 lark-server（3路由），最后迁移 monitor-dashboard（37路由，保持 Node 运行时通过 `@hono/node-server`）。

**Tech Stack:** Hono, @hono/node-server, prom-client, jsonwebtoken, TypeORM

---

## 文件变更总览

### @inner/shared (packages/ts-shared)
- Modify: `src/middleware/auth.ts` — Koa→Hono 中间件签名
- Modify: `src/middleware/error-handler.ts` — Koa→Hono 中间件签名
- Modify: `src/middleware/trace.ts` — Koa→Hono 中间件签名
- Modify: `src/middleware/validation.ts` — Koa→Hono 中间件签名
- Modify: `src/middleware/index.ts` — 更新类型导出
- Modify: `package.json` — peer dep koa→hono

### lark-server (apps/lark-server)
- Modify: `src/startup/server.ts` — Koa app→Hono app
- Modify: `src/middleware/error-handler.ts` — 适配 Hono 签名
- Modify: `src/middleware/trace.ts` — Koa ctx→Hono c
- Modify: `src/middleware/bot-context.ts` — Koa ctx→Hono c
- Modify: `src/middleware/metrics.ts` — Koa ctx→Hono c + 路由
- Modify: `src/middleware/auth.ts` — re-export 自动跟随 shared
- Modify: `src/middleware/validation.ts` — re-export 自动跟随 shared
- Modify: `src/api/routes/internal-lark.route.ts` — Koa Router→Hono
- Modify: `src/middleware/error-handler.test.ts` — 适配 Hono
- Modify: `package.json` — 移除 koa deps，加 hono

### monitor-dashboard (apps/monitor-dashboard)
- Modify: `src/index.ts` — Koa→Hono + @hono/node-server
- Modify: `src/middleware/jwt-auth.ts` — Koa→Hono
- Modify: `src/middleware/audit.ts` — Koa→Hono
- Modify: `src/routes/auth.ts` — ctx→c
- Modify: `src/routes/config.ts` — ctx→c
- Modify: `src/routes/messages.ts` — ctx→c
- Modify: `src/routes/providers.ts` — ctx→c
- Modify: `src/routes/model-mappings.ts` — ctx→c
- Modify: `src/routes/mongo.ts` — ctx→c
- Modify: `src/routes/migrations.ts` — ctx→c
- Modify: `src/routes/service-status.ts` — ctx→c
- Modify: `src/routes/operations.ts` — ctx→c
- Modify: `src/routes/audit-logs.ts` — ctx→c
- Modify: `src/routes/activity.ts` — ctx→c
- Modify: `package.json` — 移除 koa deps，加 hono + @hono/node-server

---

## Koa→Hono 转换速查表

所有路由文件遵循相同的机械替换规则：

| Koa | Hono |
|-----|------|
| `import Router from '@koa/router'` | `import { Hono } from 'hono'` |
| `const router = new Router()` | `const app = new Hono()` |
| `router.get('/path', async (ctx) => {` | `app.get('/path', async (c) => {` |
| `ctx.body = data` | `return c.json(data)` |
| `ctx.status = 404; ctx.body = { message: 'x' }; return;` | `return c.json({ message: 'x' }, 404)` |
| `ctx.params.id` | `c.req.param('id')` |
| `ctx.query.key as string` | `c.req.query('key')` |
| `ctx.query as Record<string, string>` | 逐字段 `c.req.query('field')` |
| `ctx.request.body as T` | `await c.req.json() as T` |
| `ctx.get('Header')` | `c.req.header('Header')` |
| `ctx.headers['x-lane']` | `c.req.header('x-lane')` |
| `ctx.set('Header', val)` | `c.header('Header', val)` |
| `ctx.state.caller` | `c.get('caller')` |
| `ctx.method` | `c.req.method` |
| `ctx.path` | `c.req.path` |
| `export default router` | `export default app` |

**关键差异：** Koa 用赋值 (`ctx.body = x`)，Hono 用返回 (`return c.json(x)`)。每个 handler 的最后一个 `ctx.body = x` 必须改为 `return c.json(x)`。

---

## Task 1: @inner/shared 中间件迁移

**Files:**
- Modify: `packages/ts-shared/src/middleware/auth.ts`
- Modify: `packages/ts-shared/src/middleware/error-handler.ts`
- Modify: `packages/ts-shared/src/middleware/trace.ts`
- Modify: `packages/ts-shared/src/middleware/validation.ts`
- Modify: `packages/ts-shared/src/middleware/index.ts`
- Modify: `packages/ts-shared/package.json`

- [ ] **Step 1: 更新 package.json 依赖**

将 peer dep `koa` 换为 `hono`，dev dep `@types/koa` 删除：

```json
{
  "peerDependencies": {
    "hono": "^4.0.0",
    "typeorm": "^0.3.17",
    "pg": "^8.11.3"
  },
  "peerDependenciesMeta": {
    "hono": { "optional": true },
    "typeorm": { "optional": true },
    "pg": { "optional": true }
  }
}
```

devDependencies 中删除 `"@types/koa": "^2.15.0"`。

- [ ] **Step 2: 重写 auth.ts**

```typescript
import type { Context, Next } from 'hono';

export interface BearerAuthOptions {
    getExpectedToken?: () => string | undefined;
    errorResponse?: {
        missingAuth?: { success: boolean; message: string };
        invalidToken?: { success: boolean; message: string };
    };
}

export function createBearerAuthMiddleware(options: BearerAuthOptions = {}) {
    const {
        getExpectedToken = () => process.env.INNER_HTTP_SECRET,
        errorResponse = {
            missingAuth: { success: false, message: 'Missing or invalid Authorization header' },
            invalidToken: { success: false, message: 'Invalid authentication token' },
        },
    } = options;

    return async (c: Context, next: Next) => {
        const authHeader = c.req.header('authorization');

        if (!authHeader || !authHeader.startsWith('Bearer ')) {
            return c.json(errorResponse.missingAuth, 401);
        }

        const token = authHeader.substring(7);
        const expectedToken = getExpectedToken();

        if (token !== expectedToken) {
            return c.json(errorResponse.invalidToken, 401);
        }

        await next();
    };
}

export const bearerAuthMiddleware = createBearerAuthMiddleware();
```

- [ ] **Step 3: 重写 error-handler.ts**

```typescript
import type { Context, Next } from 'hono';

export interface ErrorHandlerOptions {
    logger?: {
        warn: (message: string, meta?: Record<string, unknown>) => void;
        error: (message: string, meta?: Record<string, unknown>) => void;
    };
}

export class AppError extends Error {
    constructor(
        public statusCode: number,
        message: string,
        public isOperational = true,
    ) {
        super(message);
        this.name = 'AppError';
    }
}

export function createErrorHandler(options: ErrorHandlerOptions = {}) {
    const { logger } = options;

    return async (c: Context, next: Next): Promise<void | Response> => {
        try {
            await next();
        } catch (err: unknown) {
            const error = err as Error;

            if (error instanceof AppError) {
                logger?.warn('Operational error', { message: error.message });
                return c.json({ error: error.message, code: error.statusCode }, error.statusCode as any);
            }

            logger?.error('Unexpected error', {
                name: error?.name,
                message: error?.message,
                stack: error instanceof Error ? error.stack : undefined,
            });
            return c.json({ error: 'Internal server error', code: 500 }, 500);
        }
    };
}

export const errorHandler = createErrorHandler();
```

- [ ] **Step 4: 重写 trace.ts**

`additionalContext` 回调签名从 `(ctx: KoaContext)` 改为 `(c: HonoContext)`：

```typescript
import type { Context, Next } from 'hono';
import { asyncLocalStorage, BaseRequestContext } from './context';
import { v4 as uuidv4 } from 'uuid';

export interface TraceMiddlewareOptions {
    headerName?: string;
    responseHeaderName?: string;
    additionalContext?: (c: Context) => Partial<BaseRequestContext>;
}

export function createTraceMiddleware(options: TraceMiddlewareOptions = {}) {
    const {
        headerName = 'x-trace-id',
        responseHeaderName = 'X-Trace-Id',
        additionalContext,
    } = options;

    return async (c: Context, next: Next) => {
        const traceId = c.req.header(headerName) || uuidv4();

        const contextData: BaseRequestContext = {
            traceId,
            ...(additionalContext ? additionalContext(c) : {}),
        };

        await asyncLocalStorage.run(contextData, async () => {
            c.header(responseHeaderName, traceId);
            await next();
        });
    };
}

export const traceMiddleware = createTraceMiddleware();
```

- [ ] **Step 5: 重写 validation.ts**

`validateFields` 纯函数不变，只改中间件包装层：

```typescript
import type { Context, Next } from 'hono';

export class ValidationError extends Error {
    constructor(message: string, public field: string) {
        super(message);
        this.name = 'ValidationError';
    }
}

export interface ValidationRule {
    required?: boolean;
    type?: 'string' | 'number' | 'boolean';
    minLength?: number;
    maxLength?: number;
    pattern?: RegExp;
    custom?: (value: unknown) => boolean | string;
}

export interface ValidationRules {
    [key: string]: ValidationRule;
}

function validateType(value: unknown, type: string): boolean {
    switch (type) {
        case 'string': return typeof value === 'string';
        case 'number': return typeof value === 'number' && !isNaN(value);
        case 'boolean': return typeof value === 'boolean';
        default: return false;
    }
}

function validateFields(data: Record<string, unknown>, rules: ValidationRules): void {
    for (const [fieldName, rule] of Object.entries(rules)) {
        const value = data[fieldName];
        if (rule.required && (value === undefined || value === null || value === '')) {
            throw new ValidationError(`${fieldName} is required`, fieldName);
        }
        if (value === undefined || value === null) continue;
        if (rule.type && !validateType(value, rule.type)) {
            throw new ValidationError(`${fieldName} must be of type ${rule.type}`, fieldName);
        }
        if (rule.type === 'string' || typeof value === 'string') {
            const strValue = value as string;
            if (rule.minLength && strValue.length < rule.minLength) {
                throw new ValidationError(`${fieldName} must be at least ${rule.minLength} characters`, fieldName);
            }
            if (rule.maxLength && strValue.length > rule.maxLength) {
                throw new ValidationError(`${fieldName} must be at most ${rule.maxLength} characters`, fieldName);
            }
        }
        if (rule.pattern && typeof value === 'string' && !rule.pattern.test(value)) {
            throw new ValidationError(`${fieldName} format is invalid`, fieldName);
        }
        if (rule.custom) {
            const result = rule.custom(value);
            if (result !== true) {
                const message = typeof result === 'string' ? result : `${fieldName} validation failed`;
                throw new ValidationError(message, fieldName);
            }
        }
    }
}

export function validateBody(rules: ValidationRules) {
    return async (c: Context, next: Next) => {
        try {
            const body = (await c.req.json().catch(() => ({}))) as Record<string, unknown>;
            validateFields(body, rules);
            await next();
        } catch (error) {
            if (error instanceof ValidationError) {
                return c.json({
                    success: false,
                    message: `Validation failed: ${error.message}`,
                    field: error.field,
                    error_code: 'VALIDATION_ERROR',
                }, 400);
            }
            throw error;
        }
    };
}

export function validateQuery(rules: ValidationRules) {
    return async (c: Context, next: Next) => {
        try {
            const query = Object.fromEntries(
                new URL(c.req.url).searchParams.entries()
            ) as Record<string, unknown>;
            validateFields(query, rules);
            await next();
        } catch (error) {
            if (error instanceof ValidationError) {
                return c.json({
                    success: false,
                    message: `Query validation failed: ${error.message}`,
                    field: error.field,
                    error_code: 'VALIDATION_ERROR',
                }, 400);
            }
            throw error;
        }
    };
}
```

- [ ] **Step 6: 更新 index.ts 导出**

导出名称不变，无需修改 `index.ts`。验证导出列表与原版一致。

- [ ] **Step 7: 安装依赖并验证编译**

```bash
cd packages/ts-shared && pnpm install && pnpm build
```

- [ ] **Step 8: Commit**

```bash
git add packages/ts-shared/
git commit -m "refactor(shared): migrate middleware from Koa to Hono"
```

---

## Task 2: lark-server 迁移

**Files:**
- Modify: `apps/lark-server/package.json`
- Modify: `apps/lark-server/src/startup/server.ts`
- Modify: `apps/lark-server/src/middleware/error-handler.ts`
- Modify: `apps/lark-server/src/middleware/trace.ts`
- Modify: `apps/lark-server/src/middleware/bot-context.ts`
- Modify: `apps/lark-server/src/middleware/metrics.ts`
- Modify: `apps/lark-server/src/middleware/auth.ts`
- Modify: `apps/lark-server/src/middleware/validation.ts`
- Modify: `apps/lark-server/src/api/routes/internal-lark.route.ts`
- Modify: `apps/lark-server/src/middleware/error-handler.test.ts`

- [ ] **Step 1: 更新 package.json**

dependencies 中：
- 删除: `"koa": "^2.14.2"`, `"koa-body": "^6.0.1"`, `"@koa/cors": "^5.0.0"`, `"@koa/router": "^12.0.1"`
- 新增: `"hono": "^4.7.0"`

devDependencies 中：
- 删除: `"@types/koa": "^2.15.0"`, `"@types/koa__cors": "^5.0.0"`, `"@types/koa__router": "^12.0.4"`

- [ ] **Step 2: 重写 middleware/metrics.ts**

```typescript
import { Counter, Histogram, Gauge, Registry, collectDefaultMetrics } from 'prom-client';
import type { Context, Next } from 'hono';
import { Hono } from 'hono';

export const register = new Registry();
collectDefaultMetrics({ register });

const httpRequestsTotal = new Counter({
    name: 'http_requests_total',
    help: 'Total HTTP requests',
    labelNames: ['method', 'path', 'status'] as const,
    registers: [register],
});

const httpRequestDuration = new Histogram({
    name: 'http_request_duration_seconds',
    help: 'HTTP request duration in seconds',
    labelNames: ['method', 'path'] as const,
    registers: [register],
});

const httpRequestsInFlight = new Gauge({
    name: 'http_requests_in_flight',
    help: 'Number of HTTP requests currently being processed',
    registers: [register],
});

export async function metricsMiddleware(c: Context, next: Next): Promise<void> {
    httpRequestsInFlight.inc();
    const start = performance.now();
    try {
        await next();
    } finally {
        const duration = (performance.now() - start) / 1000;
        httpRequestsInFlight.dec();
        httpRequestsTotal.inc({ method: c.req.method, path: c.req.path, status: String(c.res.status) });
        httpRequestDuration.observe({ method: c.req.method, path: c.req.path }, duration);
    }
}

export const metricsApp = new Hono();
metricsApp.get('/metrics', async (c) => {
    return c.text(await register.metrics(), 200, {
        'Content-Type': register.contentType,
    });
});
```

- [ ] **Step 3: 重写 middleware/trace.ts**

```typescript
import type { Context, Next } from 'hono';
import { asyncLocalStorage } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';

export const traceMiddleware = async (c: Context, next: Next) => {
    const traceId = c.req.header('x-trace-id') || uuidv4();

    await asyncLocalStorage.run({ traceId }, async () => {
        c.header('X-Trace-Id', traceId);
        await next();
    });
};
```

- [ ] **Step 4: 重写 middleware/bot-context.ts**

```typescript
import type { Context, Next } from 'hono';
import { asyncLocalStorage, context } from './context';

export const botContextMiddleware = async (c: Context, next: Next) => {
    const botName = c.req.header('x-app-name') || undefined;
    const lane = c.req.header('x-lane') || undefined;

    const newStore = context.set({ botName, lane });
    await asyncLocalStorage.run(newStore, next);
};
```

- [ ] **Step 5: 重写 middleware/error-handler.ts**

```typescript
import logger from '@logger/index';
import { createErrorHandler } from '@inner/shared';

export const errorHandler = createErrorHandler({
    logger: {
        warn: (message: string, meta?: Record<string, unknown>) => {
            logger.warn(message, meta);
        },
        error: (message: string, meta?: Record<string, unknown>) => {
            logger.error(message, meta);
        },
    },
});
```

注意：删除 `import type { Context, Next } from 'koa'`，此文件不再需要类型导入（`createErrorHandler` 已返回 Hono 中间件）。

- [ ] **Step 6: 重写 api/routes/internal-lark.route.ts**

```typescript
import { Hono } from 'hono';
import { insertEvent } from '@dal/mongo/client';
import { context } from '@middleware/context';
import { EventRegistry, registerEventHandlerInstance } from '@lark/events/event-registry';
import { larkEventHandlers } from '@lark/events/handlers';

let initialized = false;
function ensureHandlersInitialized(): void {
    if (!initialized) {
        registerEventHandlerInstance(larkEventHandlers);
        initialized = true;
        console.info('Internal lark route: Event handlers initialized');
    }
}

const app = new Hono();

app.post('/api/internal/lark-event', async (c) => {
    const authHeader = c.req.header('Authorization') || '';
    const token = authHeader.replace('Bearer ', '');
    if (token !== process.env.INNER_HTTP_SECRET) {
        return c.json({ error: 'Unauthorized' }, 401);
    }

    const botName = c.req.header('X-App-Name');
    const traceId = c.req.header('x-trace-id');
    const lane = c.req.header('x-lane') || undefined;

    const { event_type, params } = await c.req.json() as {
        event_type: string;
        params: unknown;
    };

    if (!event_type || !params) {
        return c.json({ error: 'Missing event_type or params' }, 400);
    }

    insertEvent(params).catch((err) => {
        console.error('insert event error:', err);
    });

    ensureHandlersInitialized();

    const contextData = context.createContext(botName || undefined, traceId || undefined, lane);
    context.run(contextData, async () => {
        const handler = EventRegistry.getHandlerByEventType(event_type);
        if (handler) {
            handler(params).catch((err) => {
                console.error(`handler ${event_type} failed:`, err);
            });
        } else {
            console.warn(`No handler for event_type: ${event_type}`);
        }
    });

    return c.json({ ok: true });
});

export default app;
```

- [ ] **Step 7: 重写 startup/server.ts**

```typescript
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { bodyLimit } from 'hono/body-limit';
import { errorHandler } from '@middleware/error-handler';
import { traceMiddleware } from '@middleware/trace';
import { botContextMiddleware } from '@middleware/bot-context';
import { metricsMiddleware, metricsApp } from '@middleware/metrics';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import internalLarkRoutes from '@api/routes/internal-lark.route';

export interface ServerConfig {
    port: number;
}

export class HttpServerManager {
    private app: Hono;
    private config: ServerConfig;

    constructor(
        config: ServerConfig = { port: 3000 },
    ) {
        this.config = config;
        this.app = new Hono();
        this.setupMiddleware();
    }

    private setupMiddleware(): void {
        this.app.use('*', metricsMiddleware);
        this.app.use('*', cors());
        this.app.use('*', traceMiddleware);
        this.app.use('*', errorHandler);
        this.app.use('*', botContextMiddleware);
        this.app.use('*', bodyLimit({ maxSize: 50 * 1024 * 1024 }));
    }

    private registerHealthCheck(): void {
        this.app.get('/api/health', (c) => {
            try {
                const allBots = multiBotManager.getAllBotConfigs();
                return c.json({
                    status: 'ok',
                    timestamp: new Date().toISOString(),
                    service: 'lark-server',
                    version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
                    bots: allBots.map((bot) => ({
                        name: bot.bot_name,
                        app_id: bot.app_id,
                        init_type: bot.init_type,
                        is_active: bot.is_active,
                    })),
                });
            } catch (error) {
                return c.json({
                    status: 'error',
                    message: error instanceof Error ? error.message : 'Unknown error',
                }, 500);
            }
        });
    }

    async start(): Promise<void> {
        this.app.route('', metricsApp);
        this.registerHealthCheck();
        this.app.route('', internalLarkRoutes);

        // Bun native server
        Bun.serve({
            port: this.config.port,
            fetch: this.app.fetch,
        });
        console.info(`HTTP server started on port ${this.config.port}`);
        this.logAvailableRoutes();
    }

    private logAvailableRoutes(): void {
        console.info('Available routes:');
        console.info('  - /api/health (health check)');
        console.info('  - /api/internal/lark-event (lane-proxy forwarded events)');
    }

    getApp(): Hono {
        return this.app;
    }
}
```

注意：Koa 的 `koa-body` 同时做 body parsing 和 size limit。Hono 不需要显式 body parser（`c.req.json()` 按需解析），只需 `bodyLimit` 控制大小。

- [ ] **Step 8: 更新 middleware/auth.ts 和 validation.ts**

这两个文件只是 re-export shared 的内容，无需修改（import 路径不变）。验证它们仍能正常导出即可。

- [ ] **Step 9: 更新 error-handler.test.ts**

```typescript
import { describe, test, expect, beforeEach, mock } from 'bun:test';
import { AppError } from '@inner/shared';
import { Hono } from 'hono';

const mockLogger = {
    warn: mock(),
    error: mock(),
    info: mock(),
};
mock.module('@logger/index', () => ({
    default: mockLogger,
}));

const { errorHandler } = await import('@middleware/error-handler');

describe('middleware/error-handler', () => {
    beforeEach(() => {
        mockLogger.warn.mockReset();
        mockLogger.error.mockReset();
        mockLogger.info.mockReset();
    });

    test('捕获 AppError 并返回统一业务错误响应', async () => {
        const app = new Hono();
        app.use('*', errorHandler);
        app.get('/test', () => {
            throw new AppError(400, '无效的参数');
        });

        const res = await app.request('/test');
        expect(res.status).toBe(400);
        expect(await res.json()).toEqual({ error: '无效的参数', code: 400 });
        expect(mockLogger.warn).toHaveBeenCalledWith('Operational error', {
            message: '无效的参数',
        });
        expect(mockLogger.error).not.toHaveBeenCalled();
    });

    test('捕获未知错误并返回 500 与通用消息', async () => {
        const app = new Hono();
        app.use('*', errorHandler);
        app.get('/test', () => {
            throw new Error('boom');
        });

        const res = await app.request('/test');
        expect(res.status).toBe(500);
        expect(await res.json()).toEqual({ error: 'Internal server error', code: 500 });
        expect(mockLogger.error).toHaveBeenCalled();
    });
});
```

- [ ] **Step 10: 安装依赖并运行测试**

```bash
cd apps/lark-server && pnpm install && pnpm test
```

- [ ] **Step 11: 验证编译**

```bash
cd apps/lark-server && pnpm build
```

如果编译脚本用 `bun build`，确认无类型错误。

- [ ] **Step 12: Commit**

```bash
git add apps/lark-server/
git commit -m "refactor(lark-server): migrate from Koa to Hono"
```

---

## Task 3: monitor-dashboard 中间件迁移

**Files:**
- Modify: `apps/monitor-dashboard/package.json`
- Modify: `apps/monitor-dashboard/src/index.ts`
- Modify: `apps/monitor-dashboard/src/middleware/jwt-auth.ts`
- Modify: `apps/monitor-dashboard/src/middleware/audit.ts`

- [ ] **Step 1: 更新 package.json**

dependencies 中：
- 删除: `"koa": "^2.15.0"`, `"@koa/cors": "^5.0.0"`, `"@koa/router": "^12.0.1"`, `"koa-bodyparser": "^4.4.1"`
- 新增: `"hono": "^4.7.0"`, `"@hono/node-server": "^1.13.0"`

devDependencies 中：
- 删除: `"@types/koa": "^2.15.0"`, `"@types/koa__cors": "^5.0.0"`, `"@types/koa__router": "^12.0.4"`, `"@types/koa-bodyparser": "^4.3.12"`

- [ ] **Step 2: 定义 Hono Variables 类型**

在 `apps/monitor-dashboard/src/types.ts` 创建：

```typescript
export type AppVariables = {
    caller: string;
    user: unknown;
};
```

这个类型让 `c.get('caller')` 和 `c.set('caller', value)` 有类型安全。

- [ ] **Step 3: 重写 middleware/jwt-auth.ts**

```typescript
import type { Context, Next } from 'hono';
import jwt from 'jsonwebtoken';

const PUBLIC_PATHS = new Set([
  '/dashboard/api/auth/login',
  '/dashboard/api/config',
  '/dashboard/api/health',
]);

export const jwtAuth = async (c: Context, next: Next) => {
  if (!c.req.path.startsWith('/dashboard/api')) {
    return next();
  }
  if (PUBLIC_PATHS.has(c.req.path)) {
    c.set('caller', 'public');
    return next();
  }

  // API Key auth (Claude Code)
  const apiKey = c.req.header('x-api-key');
  const ccToken = process.env.DASHBOARD_CC_TOKEN;
  if (apiKey && ccToken && apiKey === ccToken) {
    c.set('caller', 'claude-code');
    return next();
  }

  // JWT Bearer auth (Web Admin)
  const authHeader = c.req.header('authorization') || c.req.header('Authorization') || '';
  const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : '';

  if (!token) {
    return c.json({ message: 'Unauthorized' }, 401);
  }

  try {
    const secret = process.env.DASHBOARD_JWT_SECRET!;
    const payload = jwt.verify(token, secret);
    c.set('user', payload);
    c.set('caller', 'web-admin');
  } catch {
    return c.json({ message: 'Unauthorized' }, 401);
  }

  await next();
};
```

- [ ] **Step 4: 重写 middleware/audit.ts**

关键差异：Hono 中 `await next()` 后响应在 `c.res`，读 status 用 `c.res.status`，读 body 需要 clone。`ctx.params` 在全局中间件中不可用，改为从路径解析。`ctx.request.body` 改为 `c.req.json()`（Hono 会缓存已解析的 body）。

```typescript
import type { Context, Next } from 'hono';
import { AppDataSource } from '../db';
import { AuditLog } from '../entities/audit-log';

function deriveAction(method: string, path: string): string {
  const p = path.replace(/^\/dashboard/, '');

  const patterns: [RegExp, string][] = [
    [/^\/api\/ops\/services\/[^/]+\/pods$/, 'ops.pods.read'],
    [/^\/api\/ops\/services$/, 'ops.services.read'],
    [/^\/api\/ops\/builds\/[^/]+\/latest$/, 'ops.builds.read'],
    [/^\/api\/ops\/db-query$/, 'ops.db-query'],
    [/^\/api\/ops\/lane-bindings$/, method === 'GET' ? 'ops.lane-bindings.read' : method === 'POST' ? 'ops.lane-bindings.create' : 'ops.lane-bindings.delete'],
    [/^\/api\/ops\/trigger-diary$/, 'ops.trigger-diary'],
    [/^\/api\/ops\/trigger-weekly-review$/, 'ops.trigger-weekly-review'],
    [/^\/api\/audit-logs$/, 'audit-logs.read'],
    [/^\/api\/activity\//, 'activity.read'],
    [/^\/api\/providers/, method === 'GET' ? 'providers.read' : `providers.${method.toLowerCase()}`],
    [/^\/api\/model-mappings/, method === 'GET' ? 'model-mappings.read' : `model-mappings.${method.toLowerCase()}`],
    [/^\/api\/messages/, 'messages.read'],
    [/^\/api\/service-status/, 'service-status.read'],
    [/^\/api\/migrations/, method === 'POST' ? 'migrations.run' : 'migrations.read'],
    [/^\/api\/mongo/, 'mongo.query'],
  ];

  for (const [re, action] of patterns) {
    if (re.test(p)) return action;
  }
  return `${method.toLowerCase()}.${p.replace(/^\/api\//, '').replace(/\//g, '.')}`;
}

const SKIP_PATHS = new Set([
  '/dashboard/api/auth/login',
  '/dashboard/api/config',
  '/dashboard/api/health',
]);

export const auditMiddleware = async (c: Context, next: Next) => {
  if (!c.req.path.startsWith('/dashboard/api') || SKIP_PATHS.has(c.req.path)) {
    return next();
  }

  const start = Date.now();
  let result: 'success' | 'error' | 'denied' = 'success';
  let errorMessage: string | null = null;

  // Pre-read body for audit logging (Hono caches parsed JSON)
  let requestBody: Record<string, unknown> | null = null;
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(c.req.method)) {
    try {
      requestBody = await c.req.json();
    } catch { /* no body or not JSON */ }
  }

  try {
    await next();
    const status = c.res.status;
    if (status === 401 || status === 403) {
      result = 'denied';
    } else if (status >= 400) {
      result = 'error';
      try {
        const resBody = await c.res.clone().json() as Record<string, unknown>;
        errorMessage = (resBody?.message as string) || `HTTP ${status}`;
      } catch {
        errorMessage = `HTTP ${status}`;
      }
    }
  } catch (err) {
    result = 'error';
    errorMessage = err instanceof Error ? err.message : String(err);
    throw err;
  } finally {
    const duration = Date.now() - start;
    const caller = c.get('caller') || 'unknown';
    const action = deriveAction(c.req.method, c.req.path);

    const params: Record<string, unknown> = {};
    const queryObj = c.req.queries();
    if (queryObj && Object.keys(queryObj).length) params.query = queryObj;
    if (requestBody && typeof requestBody === 'object' && Object.keys(requestBody).length) {
      const body = { ...requestBody };
      delete body.password;
      delete body.api_key;
      params.body = body;
    }

    try {
      const repo = AppDataSource.getRepository(AuditLog);
      await repo.save({
        caller,
        action,
        params: Object.keys(params).length ? params : null,
        result,
        error_message: errorMessage,
        duration_ms: duration,
      });
    } catch (auditErr) {
      console.error('Failed to write audit log:', auditErr);
    }
  }
};
```

- [ ] **Step 5: 重写 index.ts**

```typescript
import 'reflect-metadata';
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { serve } from '@hono/node-server';

import { AppDataSource } from './db';
import { initMongo } from './mongo';
import { jwtAuth } from './middleware/jwt-auth';
import { auditMiddleware } from './middleware/audit';

import authRoutes from './routes/auth';
import configRoutes from './routes/config';
import messagesRoutes from './routes/messages';
import providersRoutes from './routes/providers';
import modelMappingsRoutes from './routes/model-mappings';
import mongoRoutes from './routes/mongo';
import migrationsRoutes from './routes/migrations';
import serviceStatusRoutes from './routes/service-status';
import operationsRoutes from './routes/operations';
import auditLogsRoutes from './routes/audit-logs';
import activityRoutes from './routes/activity';

const PORT = Number(process.env.DASHBOARD_PORT || 3002);

if (!process.env.DASHBOARD_JWT_SECRET) {
  console.error('FATAL: DASHBOARD_JWT_SECRET is required but not set');
  process.exit(1);
}

const bootstrap = async () => {
  await AppDataSource.initialize();
  await initMongo();

  const app = new Hono();

  if (process.env.NODE_ENV !== 'production') {
    app.use('*', cors());
  }

  // Global error handler
  app.use('*', async (c, next) => {
    try {
      await next();
    } catch (err: unknown) {
      const axiosResp = (err as { response?: { status?: number; data?: unknown } })?.response;
      const status = axiosResp?.status
        || (err as { status?: number })?.status
        || 500;
      const raw = axiosResp?.data as Record<string, unknown> | undefined;
      const upstream = (raw && typeof raw === 'object' && 'data' in raw) ? raw.data as Record<string, unknown> : raw;
      const message = (upstream && typeof upstream === 'object' && 'error' in upstream)
        ? upstream.error
        : err instanceof Error ? err.message : String(err);
      return c.json({ message, status }, status as any);
    }
  });

  app.use('/dashboard/api/*', jwtAuth);
  app.use('/dashboard/api/*', auditMiddleware);

  // Mount all route sub-apps under /dashboard prefix
  const dashboard = new Hono();
  dashboard.route('', authRoutes);
  dashboard.route('', configRoutes);
  dashboard.route('', messagesRoutes);
  dashboard.route('', providersRoutes);
  dashboard.route('', modelMappingsRoutes);
  dashboard.route('', mongoRoutes);
  dashboard.route('', migrationsRoutes);
  dashboard.route('', serviceStatusRoutes);
  dashboard.route('', operationsRoutes);
  dashboard.route('', auditLogsRoutes);
  dashboard.route('', activityRoutes);

  app.route('/dashboard', dashboard);

  serve({ fetch: app.fetch, port: PORT }, () => {
    console.log(`Monitor dashboard server running on ${PORT}`);
  });
};

bootstrap().catch((err) => {
  console.error('Failed to start monitor dashboard:', err);
  process.exit(1);
});
```

注意：Koa 用 `new Router({ prefix: '/dashboard' })`，Hono 用 `app.route('/dashboard', dashboard)` 实现相同效果。路由文件内的路径保持不变（`/api/xxx`），挂载时自动加 `/dashboard` 前缀。

- [ ] **Step 6: Commit 中间件和入口**

```bash
git add apps/monitor-dashboard/src/index.ts apps/monitor-dashboard/src/middleware/ apps/monitor-dashboard/src/types.ts apps/monitor-dashboard/package.json
git commit -m "refactor(dashboard): migrate entry and middleware from Koa to Hono"
```

---

## Task 4: monitor-dashboard 路由迁移（简单路由）

**Files:**
- Modify: `apps/monitor-dashboard/src/routes/auth.ts`
- Modify: `apps/monitor-dashboard/src/routes/config.ts`
- Modify: `apps/monitor-dashboard/src/routes/service-status.ts`
- Modify: `apps/monitor-dashboard/src/routes/audit-logs.ts`
- Modify: `apps/monitor-dashboard/src/routes/mongo.ts`
- Modify: `apps/monitor-dashboard/src/routes/migrations.ts`

这些路由文件较短（< 70 行），转换直接。

- [ ] **Step 1: 重写 auth.ts**

```typescript
import { Hono } from 'hono';
import jwt from 'jsonwebtoken';

const app = new Hono();

app.post('/api/auth/login', async (c) => {
  const { password } = await c.req.json() as { password?: string };

  if (!password || password !== process.env.DASHBOARD_ADMIN_PASSWORD) {
    return c.json({ message: 'Invalid password' }, 401);
  }

  const secret = process.env.DASHBOARD_JWT_SECRET!;
  const token = jwt.sign({ role: 'admin' }, secret, { expiresIn: '7d' });

  return c.json({ token });
});

export default app;
```

- [ ] **Step 2: 重写 config.ts**

```typescript
import { Hono } from 'hono';

const app = new Hono();

app.get('/api/config', async (c) => {
  const grafanaHost = process.env.DASHBOARD_GRAFANA_HOST || '';
  const langfuseHost = process.env.DASHBOARD_LANGFUSE_HOST || '';
  const langfuseProjectId = process.env.DASHBOARD_LANGFUSE_PROJECT_ID || '';

  return c.json({
    grafanaUrl: grafanaHost,
    kibanaUrl: grafanaHost,
    langfuseUrl: `${langfuseHost}/project/${langfuseProjectId}`,
  });
});

app.get('/api/health', async (c) => {
  return c.json({
    status: 'ok',
    timestamp: new Date().toISOString(),
    service: 'monitor-dashboard',
    version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
  });
});

export default app;
```

- [ ] **Step 3: 重写 service-status.ts**

```typescript
import { Hono } from 'hono';
import axios from 'axios';

const app = new Hono();

app.get('/api/service-status', async (c) => {
  const paasApi = process.env.DASHBOARD_PAAS_API;
  const paasToken = process.env.DASHBOARD_PAAS_TOKEN;

  if (!paasApi || !paasToken) {
    return c.json({ message: 'DASHBOARD_PAAS_API or DASHBOARD_PAAS_TOKEN not configured' }, 500);
  }

  const headers = { 'X-API-Key': paasToken };
  const timeout = 10000;

  const [appsRes, releasesRes] = await Promise.all([
    axios.get(`${paasApi}/api/paas/apps/`, { headers, timeout }),
    axios.get(`${paasApi}/api/paas/releases/`, { headers, timeout }),
  ]);

  return c.json({
    apps: appsRes.data?.data ?? appsRes.data,
    releases: releasesRes.data?.data ?? releasesRes.data,
  });
});

export default app;
```

- [ ] **Step 4: 重写 audit-logs.ts**

```typescript
import { Hono } from 'hono';
import { AppDataSource } from '../db';
import { AuditLog } from '../entities/audit-log';

const app = new Hono();

app.get('/api/audit-logs', async (c) => {
  const caller = c.req.query('caller');
  const action = c.req.query('action');
  const result = c.req.query('result');
  const from = c.req.query('from');
  const to = c.req.query('to');
  const page = c.req.query('page') || '1';
  const pageSize = c.req.query('pageSize') || '50';

  const repo = AppDataSource.getRepository(AuditLog);
  const qb = repo.createQueryBuilder('log');

  if (caller) qb.andWhere('log.caller = :caller', { caller });
  if (action) qb.andWhere('log.action LIKE :action', { action: `%${action}%` });
  if (result) qb.andWhere('log.result = :result', { result });
  if (from) qb.andWhere('log.created_at >= :from', { from });
  if (to) qb.andWhere('log.created_at <= :to', { to });

  const limit = Math.min(Number(pageSize) || 50, 200);
  const offset = ((Number(page) || 1) - 1) * limit;

  qb.orderBy('log.created_at', 'DESC').skip(offset).take(limit);

  const [items, total] = await qb.getManyAndCount();

  return c.json({
    items,
    total,
    page: Number(page) || 1,
    pageSize: limit,
  });
});

export default app;
```

- [ ] **Step 5: 重写 mongo.ts**

```typescript
import { Hono } from 'hono';
import { queryLarkEvents } from '../mongo';

const app = new Hono();

const parseNumber = (value: unknown, fallback: number) => {
  if (typeof value === 'number') return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? fallback : parsed;
  }
  return fallback;
};

app.post('/api/mongo/query', async (c) => {
  const body = (await c.req.json().catch(() => ({}))) as {
    filter?: Record<string, unknown>;
    projection?: Record<string, unknown>;
    sort?: Record<string, unknown>;
    page?: number;
    pageSize?: number;
  };

  const page = Math.max(1, parseNumber(body.page, 1));
  const pageSize = Math.min(100, Math.max(1, parseNumber(body.pageSize, 20)));

  try {
    const { data, total } = await queryLarkEvents({
      filter: body.filter ?? {},
      projection: body.projection ?? {},
      sort: body.sort ?? { created_at: -1 },
      skip: (page - 1) * pageSize,
      limit: pageSize,
    });

    return c.json({ data, total, page, pageSize });
  } catch (error) {
    return c.json({ message: (error as Error).message }, 400);
  }
});

export default app;
```

- [ ] **Step 6: 重写 migrations.ts**

```typescript
import { Hono } from 'hono';
import { AppDataSource, SchemaMigration } from '../db';

const app = new Hono();

app.get('/api/migrations', async (c) => {
  const repo = AppDataSource.getRepository(SchemaMigration);
  const migrations = await repo.find({ order: { id: 'DESC' } });
  return c.json(migrations);
});

app.post('/api/migrations/run', async (c) => {
  const { version, name, sql, applied_by } = await c.req.json() as {
    version?: string;
    name?: string;
    sql?: string;
    applied_by?: string;
  };

  if (!version || !name || !sql) {
    return c.json({ message: 'version, name, sql are required' }, 400);
  }

  const repo = AppDataSource.getRepository(SchemaMigration);

  const existing = await repo.findOne({ where: { version } });
  if (existing) {
    return c.json({ message: `Migration ${version} already applied` }, 409);
  }

  const startTime = Date.now();
  let status: 'success' | 'failed' = 'success';
  let errorMessage: string | null = null;

  try {
    await AppDataSource.transaction(async (manager) => {
      await manager.query(sql);
    });
  } catch (err) {
    status = 'failed';
    errorMessage = err instanceof Error ? err.message : String(err);
  }

  const durationMs = Date.now() - startTime;

  const record = repo.create({
    version,
    name,
    sql_content: sql,
    applied_by: applied_by || 'manual',
    status,
    error_message: errorMessage,
    duration_ms: durationMs,
  });

  await repo.save(record);

  if (status === 'failed') {
    return c.json({ message: errorMessage, record }, 500);
  }

  return c.json(record);
});

export default app;
```

- [ ] **Step 7: Commit**

```bash
git add apps/monitor-dashboard/src/routes/auth.ts apps/monitor-dashboard/src/routes/config.ts apps/monitor-dashboard/src/routes/service-status.ts apps/monitor-dashboard/src/routes/audit-logs.ts apps/monitor-dashboard/src/routes/mongo.ts apps/monitor-dashboard/src/routes/migrations.ts
git commit -m "refactor(dashboard): migrate simple routes from Koa to Hono"
```

---

## Task 5: monitor-dashboard 路由迁移（复杂路由）

**Files:**
- Modify: `apps/monitor-dashboard/src/routes/providers.ts`
- Modify: `apps/monitor-dashboard/src/routes/model-mappings.ts`
- Modify: `apps/monitor-dashboard/src/routes/operations.ts`
- Modify: `apps/monitor-dashboard/src/routes/messages.ts`
- Modify: `apps/monitor-dashboard/src/routes/activity.ts`

- [ ] **Step 1: 重写 providers.ts**

```typescript
import { Hono } from 'hono';
import { AppDataSource, ModelProvider } from '../db';
import { randomUUID } from 'crypto';

const app = new Hono();

const maskApiKey = (apiKey: string) => {
  if (!apiKey) return '';
  const tail = apiKey.slice(-4);
  return `****${tail}`;
};

const allowedClientTypes = new Set(['openai', 'openai-responses', 'deepseek', 'ark', 'azure-http', 'google']);

app.get('/api/providers', async (c) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const providers = await repo.find({ order: { created_at: 'DESC' } });
  return c.json(providers.map((provider) => ({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  })));
});

app.get('/api/providers/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: c.req.param('id') } });
  if (!provider) {
    return c.json({ message: 'Not found' }, 404);
  }
  return c.json({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  });
});

app.post('/api/providers', async (c) => {
  const { name, api_key, base_url, client_type, is_active } = await c.req.json() as {
    name?: string;
    api_key?: string;
    base_url?: string;
    client_type?: string;
    is_active?: boolean;
  };

  if (!name || !base_url || !api_key) {
    return c.json({ message: 'name, base_url, api_key are required' }, 400);
  }

  const resolvedClientType = (client_type || 'openai').toLowerCase();
  if (!allowedClientTypes.has(resolvedClientType)) {
    return c.json({ message: 'Invalid client_type' }, 400);
  }

  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = repo.create({
    provider_id: randomUUID(),
    name,
    api_key,
    base_url,
    client_type: resolvedClientType,
    is_active: is_active ?? true,
  });

  await repo.save(provider);

  return c.json({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  });
});

app.put('/api/providers/:id', async (c) => {
  const { name, api_key, base_url, client_type, is_active } = await c.req.json() as {
    name?: string;
    api_key?: string;
    base_url?: string;
    client_type?: string;
    is_active?: boolean;
  };

  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: c.req.param('id') } });
  if (!provider) {
    return c.json({ message: 'Not found' }, 404);
  }

  if (name !== undefined) provider.name = name;
  if (api_key !== undefined && api_key !== '') provider.api_key = api_key;
  if (base_url !== undefined) provider.base_url = base_url;
  if (client_type !== undefined) {
    const resolvedClientType = client_type.toLowerCase();
    if (!allowedClientTypes.has(resolvedClientType)) {
      return c.json({ message: 'Invalid client_type' }, 400);
    }
    provider.client_type = resolvedClientType;
  }
  if (is_active !== undefined) provider.is_active = is_active;

  await repo.save(provider);

  return c.json({
    ...provider,
    api_key: maskApiKey(provider.api_key),
  });
});

app.delete('/api/providers/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelProvider);
  const provider = await repo.findOne({ where: { provider_id: c.req.param('id') } });
  if (!provider) {
    return c.json({ message: 'Not found' }, 404);
  }

  await repo.remove(provider);
  return c.json({ success: true });
});

export default app;
```

- [ ] **Step 2: 重写 model-mappings.ts**

```typescript
import { Hono } from 'hono';
import { AppDataSource, ModelMapping } from '../db';

const app = new Hono();

app.get('/api/model-mappings', async (c) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mappings = await repo.find({ order: { created_at: 'DESC' } });
  return c.json(mappings);
});

app.get('/api/model-mappings/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: c.req.param('id') } });
  if (!mapping) {
    return c.json({ message: 'Not found' }, 404);
  }
  return c.json(mapping);
});

app.post('/api/model-mappings', async (c) => {
  const { alias, provider_name, real_model_name, description, model_config } =
    await c.req.json() as {
      alias?: string;
      provider_name?: string;
      real_model_name?: string;
      description?: string;
      model_config?: Record<string, unknown> | null;
    };

  if (!alias || !provider_name || !real_model_name) {
    return c.json({ message: 'alias, provider_name, real_model_name are required' }, 400);
  }

  const repo = AppDataSource.getRepository(ModelMapping);
  const existing = await repo.findOne({ where: { alias } });
  if (existing) {
    return c.json({ message: 'alias already exists' }, 400);
  }

  const mapping = repo.create({
    alias,
    provider_name,
    real_model_name,
    description: description ?? null,
    model_config: model_config ?? null,
  });

  await repo.save(mapping);
  return c.json(mapping);
});

app.put('/api/model-mappings/:id', async (c) => {
  const { alias, provider_name, real_model_name, description, model_config } =
    await c.req.json() as {
      alias?: string;
      provider_name?: string;
      real_model_name?: string;
      description?: string;
      model_config?: Record<string, unknown> | null;
    };

  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: c.req.param('id') } });
  if (!mapping) {
    return c.json({ message: 'Not found' }, 404);
  }

  if (alias !== undefined && alias !== mapping.alias) {
    const existing = await repo.findOne({ where: { alias } });
    if (existing) {
      return c.json({ message: 'alias already exists' }, 400);
    }
    mapping.alias = alias;
  }
  if (provider_name !== undefined) mapping.provider_name = provider_name;
  if (real_model_name !== undefined) mapping.real_model_name = real_model_name;
  if (description !== undefined) mapping.description = description;
  if (model_config !== undefined) mapping.model_config = model_config;

  await repo.save(mapping);
  return c.json(mapping);
});

app.delete('/api/model-mappings/:id', async (c) => {
  const repo = AppDataSource.getRepository(ModelMapping);
  const mapping = await repo.findOne({ where: { id: c.req.param('id') } });
  if (!mapping) {
    return c.json({ message: 'Not found' }, 404);
  }

  await repo.remove(mapping);
  return c.json({ success: true });
});

export default app;
```

- [ ] **Step 3: 重写 operations.ts**

```typescript
import { Hono } from 'hono';
import { paasClient, larkClient } from '../paas-client';

const app = new Hono();

// ---------- 读操作 ----------

app.get('/api/ops/services', async (c) => {
  const [apps, releases] = await Promise.all([
    paasClient.get('/api/paas/apps/'),
    paasClient.get('/api/paas/releases/'),
  ]);
  return c.json({ apps, releases });
});

app.get('/api/ops/services/:app/pods', async (c) => {
  const appName = c.req.param('app');
  const lane = c.req.query('lane') || 'prod';

  const releases = (await paasClient.get('/api/paas/releases/', { app: appName, lane })) as Array<{ id: string }>;
  if (!Array.isArray(releases) || releases.length === 0) {
    return c.json({ message: `No release found for ${appName} in lane ${lane}` }, 404);
  }

  const status = await paasClient.get(`/api/paas/releases/${releases[0].id}/status`);
  return c.json(status);
});

app.get('/api/ops/builds/:app/latest', async (c) => {
  const appName = c.req.param('app');
  const data = await paasClient.get(`/api/paas/apps/${appName}/builds/latest`);
  return c.json(data);
});

app.post('/api/ops/db-query', async (c) => {
  const { sql, db } = await c.req.json() as { sql?: string; db?: string };
  if (!sql) {
    return c.json({ message: 'sql is required' }, 400);
  }

  const normalized = sql.trim().toUpperCase();
  const forbidden = ['INSERT', 'UPDATE', 'DELETE', 'DROP', 'ALTER', 'TRUNCATE', 'CREATE', 'GRANT', 'REVOKE'];
  if (forbidden.some((kw) => normalized.startsWith(kw))) {
    return c.json({ message: 'Only SELECT queries are allowed' }, 403);
  }

  const data = await paasClient.post('/api/paas/ops/query', {
    sql,
    db: db || 'paas_engine',
  });
  return c.json(data);
});

app.get('/api/ops/lane-bindings', async (c) => {
  const data = await larkClient.get('/api/lark/lane-bindings');
  return c.json(data);
});

// ---------- DDL/DML 变更审批 ----------

function laneHeaders(c: { req: { header: (name: string) => string | undefined } }): Record<string, string> | undefined {
  const lane = c.req.header('x-lane');
  return lane ? { 'x-lane': lane } : undefined;
}

app.post('/api/ops/db-mutations', async (c) => {
  const data = await paasClient.post('/api/paas/ops/mutations', await c.req.json(), laneHeaders(c));
  return c.json(data);
});

app.get('/api/ops/db-mutations', async (c) => {
  const params: Record<string, string> = {};
  const status = c.req.query('status');
  if (status) params.status = status;
  const data = await paasClient.get('/api/paas/ops/mutations', params, laneHeaders(c));
  return c.json(data);
});

app.get('/api/ops/db-mutations/:id', async (c) => {
  const data = await paasClient.get(`/api/paas/ops/mutations/${c.req.param('id')}`, undefined, laneHeaders(c));
  return c.json(data);
});

app.post('/api/ops/db-mutations/:id/approve', async (c) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${c.req.param('id')}/approve`, await c.req.json(), laneHeaders(c));
  return c.json(data);
});

app.post('/api/ops/db-mutations/:id/reject', async (c) => {
  const data = await paasClient.post(`/api/paas/ops/mutations/${c.req.param('id')}/reject`, await c.req.json(), laneHeaders(c));
  return c.json(data);
});

// ---------- 写操作 ----------

app.post('/api/ops/lane-bindings', async (c) => {
  const { route_type, route_key, lane_name } = await c.req.json() as {
    route_type?: string;
    route_key?: string;
    lane_name?: string;
  };
  if (!route_type || !route_key || !lane_name) {
    return c.json({ message: 'route_type, route_key, and lane_name are required' }, 400);
  }
  const data = await larkClient.post('/api/lark/lane-bindings', {
    route_type,
    route_key,
    lane_name,
  });
  return c.json(data);
});

app.delete('/api/ops/lane-bindings', async (c) => {
  const type = c.req.query('type');
  const key = c.req.query('key');
  if (!type || !key) {
    return c.json({ message: 'type and key query params are required' }, 400);
  }
  const data = await larkClient.del('/api/lark/lane-bindings', { type, key });
  return c.json(data);
});

app.post('/api/ops/trigger-diary', async (c) => {
  const { chat_id, target_date } = await c.req.json() as {
    chat_id?: string;
    target_date?: string;
  };
  if (!chat_id) {
    return c.json({ message: 'chat_id is required' }, 400);
  }
  const params: Record<string, string> = { chat_id };
  if (target_date) params.target_date = target_date;

  const data = await paasClient.post(
    `/api/agent/admin/trigger-diary?${new URLSearchParams(params).toString()}`,
  );
  return c.json(data);
});

app.post('/api/ops/trigger-weekly-review', async (c) => {
  const { chat_id, week_start } = await c.req.json() as {
    chat_id?: string;
    week_start?: string;
  };
  if (!chat_id) {
    return c.json({ message: 'chat_id is required' }, 400);
  }
  const params: Record<string, string> = { chat_id };
  if (week_start) params.week_start = week_start;

  const data = await paasClient.post(
    `/api/agent/admin/trigger-weekly-review?${new URLSearchParams(params).toString()}`,
  );
  return c.json(data);
});

export default app;
```

- [ ] **Step 4: 重写 messages.ts**

```typescript
import { Hono } from 'hono';
import { AppDataSource, ConversationMessage, LarkUser, LarkGroupChatInfo } from '../db';

const app = new Hono();

const parseNumber = (value: string | undefined, defaultValue: number) => {
  if (!value) return defaultValue;
  const parsed = Number(value);
  return Number.isNaN(parsed) ? defaultValue : parsed;
};

app.get('/api/messages', async (c) => {
  const page = Math.max(1, parseNumber(c.req.query('page'), 1));
  const pageSize = Math.min(100, Math.max(1, parseNumber(c.req.query('pageSize'), 20)));
  const chatId = c.req.query('chatId') || '';
  const userId = c.req.query('userId') || '';
  const role = c.req.query('role') || '';
  const botName = c.req.query('botName') || '';
  const startTime = c.req.query('startTime') || '';
  const endTime = c.req.query('endTime') || '';
  const rootMessageId = c.req.query('rootMessageId') || '';
  const replyMessageId = c.req.query('replyMessageId') || '';
  const messageType = c.req.query('messageType') || '';

  const repo = AppDataSource.getRepository(ConversationMessage);
  const qb = repo
    .createQueryBuilder('msg')
    .select([
      'msg.*',
      `CASE WHEN msg.role = 'assistant' THEN '赤尾' ELSE COALESCE(lu.name, msg.user_id) END AS user_name`,
      'gc.name AS group_name',
    ])
    .leftJoin('lark_user', 'lu', 'msg.user_id = lu.union_id')
    .leftJoin('lark_group_chat_info', 'gc', 'msg.chat_id = gc.chat_id');

  if (chatId) qb.andWhere('msg.chat_id = :chatId', { chatId });
  if (userId) qb.andWhere('msg.user_id = :userId', { userId });
  if (role) qb.andWhere('msg.role = :role', { role });
  if (botName) qb.andWhere('msg.bot_name = :botName', { botName });
  if (startTime) qb.andWhere('msg.create_time >= :startTime', { startTime });
  if (endTime) qb.andWhere('msg.create_time <= :endTime', { endTime });
  if (rootMessageId) qb.andWhere('msg.root_message_id = :rootMessageId', { rootMessageId });
  if (replyMessageId) qb.andWhere('msg.reply_message_id = :replyMessageId', { replyMessageId });
  if (messageType) qb.andWhere('msg.message_type = :messageType', { messageType });

  const countResult = await qb
    .clone()
    .select('COUNT(*)', 'count')
    .getRawOne();
  const total = parseInt(countResult?.count ?? '0', 10);

  qb.orderBy('msg.create_time', 'DESC');
  qb.offset((page - 1) * pageSize).limit(pageSize);
  const rows = await qb.getRawMany();

  const p2pChatIds = [
    ...new Set(
      rows
        .filter((r) => r.chat_type === 'p2p')
        .map((r) => r.chat_id)
    ),
  ];

  let p2pNameMap: Record<string, string> = {};
  if (p2pChatIds.length > 0) {
    const p2pRows: { chat_id: string; user_name: string }[] = await AppDataSource.query(
      `SELECT DISTINCT ON (cm.chat_id)
         cm.chat_id,
         COALESCE(lu.name, cm.user_id) AS user_name
       FROM conversation_messages cm
       LEFT JOIN lark_user lu ON cm.user_id = lu.union_id
       WHERE cm.chat_id = ANY($1) AND cm.role = 'user'
       ORDER BY cm.chat_id, cm.create_time DESC`,
      [p2pChatIds]
    );
    for (const r of p2pRows) {
      p2pNameMap[r.chat_id] = r.user_name;
    }
  }

  const data = rows.map((row) => {
    let chat_name: string;
    if (row.chat_type === 'group') {
      chat_name = row.group_name || row.chat_id;
    } else {
      const userName = p2pNameMap[row.chat_id];
      chat_name = userName ? `和${userName}的私聊会话` : row.chat_id;
    }
    const { group_name, ...rest } = row;
    return { ...rest, chat_name };
  });

  return c.json({ data, total, page, pageSize });
});

app.get('/api/chats', async (c) => {
  const keyword = (c.req.query('keyword') || '').trim();
  const repo = AppDataSource.getRepository(LarkGroupChatInfo);
  const qb = repo.createQueryBuilder('gc').select(['gc.chat_id AS chat_id', 'gc.name AS name']);
  if (keyword) {
    qb.where('gc.name ILIKE :kw', { kw: `%${keyword}%` });
  }
  qb.orderBy('gc.name', 'ASC').limit(30);
  return c.json(await qb.getRawMany());
});

app.get('/api/users', async (c) => {
  const keyword = (c.req.query('keyword') || '').trim();
  const repo = AppDataSource.getRepository(LarkUser);
  const qb = repo.createQueryBuilder('u').select(['u.union_id AS user_id', 'u.name AS name']);
  if (keyword) {
    qb.where('u.name ILIKE :kw', { kw: `%${keyword}%` });
  }
  qb.orderBy('u.name', 'ASC').limit(30);
  return c.json(await qb.getRawMany());
});

export default app;
```

- [ ] **Step 5: 重写 activity.ts**

```typescript
import { Hono } from 'hono';
import { AppDataSource, ConversationMessage, LarkGroupChatInfo, DiaryEntry, WeeklyReview } from '../db';

const app = new Hono();

function msAgo(days: number): string {
  return String(Date.now() - days * 86400000);
}

function todayStartMs(): string {
  const d = new Date();
  d.setHours(0, 0, 0, 0);
  return String(d.getTime());
}

function msToDateStr(ms: string | number): string {
  return new Date(Number(ms)).toISOString().slice(0, 10);
}

app.get('/api/activity/overview', async (c) => {
  const days = Number(c.req.query('days')) || 7;
  const since = msAgo(days);
  const todayMs = todayStartMs();

  const repo = AppDataSource.getRepository(ConversationMessage);

  const rows = await repo
    .createQueryBuilder('cm')
    .select(['cm.chat_id', 'cm.create_time', 'cm.role'])
    .where('cm.create_time >= :since', { since })
    .getMany();

  const todayChats = new Set<string>();
  let todayTotal = 0;
  let todayBotReplies = 0;
  for (const row of rows) {
    if (Number(row.create_time) >= Number(todayMs)) {
      todayTotal++;
      todayChats.add(row.chat_id);
      if (row.role === 'assistant') todayBotReplies++;
    }
  }

  const chatIds = [...new Set(rows.map((r) => r.chat_id))];
  const groupInfos = chatIds.length > 0
    ? await AppDataSource.getRepository(LarkGroupChatInfo)
        .createQueryBuilder('g')
        .where('g.chat_id IN (:...chatIds)', { chatIds })
        .getMany()
    : [];
  const nameMap = new Map(groupInfos.map((g) => [g.chat_id, g.name]));

  const groupMap = new Map<string, {
    chat_id: string;
    group_name: string;
    message_count: number;
    bot_replies: number;
    dailyMap: Map<string, number>;
  }>();

  for (const row of rows) {
    let group = groupMap.get(row.chat_id);
    if (!group) {
      group = {
        chat_id: row.chat_id,
        group_name: nameMap.get(row.chat_id) || row.chat_id,
        message_count: 0,
        bot_replies: 0,
        dailyMap: new Map(),
      };
      groupMap.set(row.chat_id, group);
    }
    group.message_count++;
    if (row.role === 'assistant') group.bot_replies++;
    const dateStr = msToDateStr(row.create_time);
    group.dailyMap.set(dateStr, (group.dailyMap.get(dateStr) || 0) + 1);
  }

  const groups = [...groupMap.values()]
    .filter((group) => group.bot_replies > 0)
    .sort((a, b) => b.bot_replies - a.bot_replies || b.message_count - a.message_count)
    .map(({ dailyMap, ...rest }) => ({
      ...rest,
      daily_counts: [...dailyMap.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([date, count]) => ({ date, count })),
    }));

  return c.json({
    summary: {
      period_total: rows.length,
      today_total: todayTotal,
      today_bot_replies: todayBotReplies,
      today_active_groups: todayChats.size,
    },
    groups,
  });
});

app.get('/api/activity/diary-status', async (c) => {
  const diaryRepo = AppDataSource.getRepository(DiaryEntry);
  const weeklyRepo = AppDataSource.getRepository(WeeklyReview);

  const sevenDaysAgo = new Date();
  sevenDaysAgo.setDate(sevenDaysAgo.getDate() - 7);
  const sevenDaysAgoStr = sevenDaysAgo.toISOString().slice(0, 10);

  const fourWeeksAgo = new Date();
  fourWeeksAgo.setDate(fourWeeksAgo.getDate() - 28);
  const fourWeeksAgoStr = fourWeeksAgo.toISOString().slice(0, 10);

  const diaries = await diaryRepo
    .createQueryBuilder('de')
    .select(['de.chat_id', 'de.diary_date', 'de.content'])
    .getMany();

  const diaryMap = new Map<string, { latest_diary_date: string; latest_diary_content: string; diary_count_7d: number }>();
  for (const d of diaries) {
    const existing = diaryMap.get(d.chat_id);
    const isRecent = d.diary_date >= sevenDaysAgoStr;
    if (!existing) {
      diaryMap.set(d.chat_id, {
        latest_diary_date: d.diary_date,
        latest_diary_content: d.content,
        diary_count_7d: isRecent ? 1 : 0,
      });
    } else {
      if (d.diary_date > existing.latest_diary_date) {
        existing.latest_diary_date = d.diary_date;
        existing.latest_diary_content = d.content;
      }
      if (isRecent) existing.diary_count_7d++;
    }
  }

  const weeklies = await weeklyRepo
    .createQueryBuilder('wr')
    .select(['wr.chat_id', 'wr.week_start', 'wr.content'])
    .getMany();

  const weeklyMap = new Map<string, { latest_week_start: string; latest_weekly_content: string; review_count_4w: number }>();
  for (const w of weeklies) {
    const existing = weeklyMap.get(w.chat_id);
    const isRecent = w.week_start >= fourWeeksAgoStr;
    if (!existing) {
      weeklyMap.set(w.chat_id, {
        latest_week_start: w.week_start,
        latest_weekly_content: w.content,
        review_count_4w: isRecent ? 1 : 0,
      });
    } else {
      if (w.week_start > existing.latest_week_start) {
        existing.latest_week_start = w.week_start;
        existing.latest_weekly_content = w.content;
      }
      if (isRecent) existing.review_count_4w++;
    }
  }

  return c.json({
    diary: [...diaryMap.entries()].map(([chat_id, v]) => ({ chat_id, ...v })),
    weekly: [...weeklyMap.entries()].map(([chat_id, v]) => ({ chat_id, ...v })),
  });
});

export default app;
```

- [ ] **Step 6: 安装依赖并验证编译**

```bash
cd apps/monitor-dashboard && pnpm install && npx tsc --noEmit
```

- [ ] **Step 7: Commit**

```bash
git add apps/monitor-dashboard/src/routes/
git commit -m "refactor(dashboard): migrate complex routes from Koa to Hono"
```

---

## Task 6: 清理和最终验证

**Files:**
- Verify: 全部已改文件

- [ ] **Step 1: 验证 monorepo 零 Koa 依赖**

```bash
# 检查所有 package.json 中是否还有 koa 相关依赖
grep -r '"koa' apps/*/package.json packages/*/package.json
# 期望：无输出
```

- [ ] **Step 2: 验证所有 import 已清理**

```bash
# 检查源码中是否还有 koa 相关 import
grep -rn "from 'koa'" apps/ packages/ --include='*.ts' --exclude-dir=node_modules
grep -rn "from '@koa/" apps/ packages/ --include='*.ts' --exclude-dir=node_modules
# 期望：无输出
```

- [ ] **Step 3: 重新安装依赖**

```bash
pnpm install
```

- [ ] **Step 4: lark-server 测试**

```bash
cd apps/lark-server && pnpm test
```

- [ ] **Step 5: monitor-dashboard 编译验证**

```bash
cd apps/monitor-dashboard && npx tsc --noEmit
```

- [ ] **Step 6: Commit 清理**

如果有遗留的 Koa 引用需要清理：

```bash
git add -A && git commit -m "chore: remove all remaining Koa references"
```
