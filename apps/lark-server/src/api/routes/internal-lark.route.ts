import Router from '@koa/router';
import { Context } from 'koa';
import { insertEvent } from '@dal/mongo/client';
import { context } from '@middleware/context';
import { EventRegistry, registerEventHandlerInstance } from '@lark/events/event-registry';
import { larkEventHandlers } from '@lark/events/handlers';

// 确保事件处理器已注册
let initialized = false;
function ensureHandlersInitialized(): void {
    if (!initialized) {
        registerEventHandlerInstance(larkEventHandlers);
        initialized = true;
        console.info('Internal lark route: Event handlers initialized');
    }
}

const router = new Router();

/**
 * 内部接口：接收 lane-proxy 转发的 Lark 事件
 * POST /api/internal/lark-event
 *
 * Headers:
 *   Authorization: Bearer {INNER_HTTP_SECRET}
 *   X-App-Name: {bot_name}
 *   x-trace-id: {trace_id}
 *
 * Body:
 *   { event_type: string, params: any }
 */
router.post('/api/internal/lark-event', async (ctx: Context) => {
    // 1. 验证 Authorization
    const authHeader = ctx.get('Authorization') || '';
    const token = authHeader.replace('Bearer ', '');
    if (token !== process.env.INNER_HTTP_SECRET) {
        ctx.status = 401;
        ctx.body = { error: 'Unauthorized' };
        return;
    }

    // 2. 从 header 提取上下文
    const botName = ctx.get('X-App-Name');
    const traceId = ctx.get('x-trace-id');

    // 3. 从 body 提取事件数据
    const { event_type, params } = ctx.request.body as {
        event_type: string;
        params: unknown;
    };

    if (!event_type || !params) {
        ctx.status = 400;
        ctx.body = { error: 'Missing event_type or params' };
        return;
    }

    // 4. MongoDB 审计日志（fire-and-forget）
    insertEvent(params).catch((err) => {
        console.error('insert event error:', err);
    });

    // 5. 确保 handler 已初始化
    ensureHandlersInitialized();

    // 6. 在 bot 上下文中异步执行 handler
    const contextData = context.createContext(botName || undefined, traceId || undefined);
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

    // 7. 立即返回
    ctx.status = 200;
    ctx.body = { ok: true };
});

export default router;
