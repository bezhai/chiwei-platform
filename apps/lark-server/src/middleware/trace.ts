import { Context, Next } from 'koa';
import { asyncLocalStorage } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';

export const traceMiddleware = async (ctx: Context, next: Next) => {
    const traceId = (ctx.request.headers['x-trace-id'] as string) || uuidv4();

    // 在AsyncLocalStorage上下文中执行整个后续的中间件链
    await asyncLocalStorage.run({ traceId }, async () => {
        ctx.set('X-Trace-Id', traceId);
        await next();
    });
};
