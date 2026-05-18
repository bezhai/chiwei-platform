import type { Context, Next } from 'hono';
import { asyncLocalStorage } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';

export const traceMiddleware = async (c: Context, next: Next) => {
    const traceId = c.req.header('x-trace-id') || uuidv4();

    // 在AsyncLocalStorage上下文中执行整个后续的中间件链
    await asyncLocalStorage.run({ traceId }, async () => {
        c.header('X-Trace-Id', traceId);
        await next();
    });
};
