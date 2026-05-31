import { Hono } from 'hono';
import { dispatchLarkEvent } from '@plugins/lark/webhook/dispatch';

const app = new Hono();

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
app.post('/api/internal/lark-event', async (c) => {
    // 1. 验证 Authorization
    const authHeader = c.req.header('Authorization') || '';
    const token = authHeader.replace('Bearer ', '');
    if (token !== process.env.INNER_HTTP_SECRET) {
        return c.json({ error: 'Unauthorized' }, 401);
    }

    // 2. 从 header 提取上下文
    const botName = c.req.header('X-App-Name');
    const traceId = c.req.header('x-trace-id');
    const lane = c.req.header('x-ctx-lane') || c.req.header('x-lane') || undefined;

    // 3. 从 body 提取事件数据
    const { event_type, params } = await c.req.json() as {
        event_type: string;
        params: unknown;
    };

    if (!event_type || !params) {
        return c.json({ error: 'Missing event_type or params' }, 400);
    }

    // 4. 统一收口：审计落库、handler 初始化与 context 分发由 dispatchLarkEvent 负责。
    await dispatchLarkEvent({
        eventType: event_type,
        params,
        botName: botName || undefined,
        traceId: traceId || undefined,
        lane,
    });

    // 5. 立即返回
    return c.json({ ok: true }, 200);
});

export default app;
