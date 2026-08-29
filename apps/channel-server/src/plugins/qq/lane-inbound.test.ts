// 泳道信封的 HTTP 接收端。
//
// 这条端点收的是「prod 已经判定该由泳道 X 处理」的信封，与 /api/internal/qq/inbound
// （qq-gateway 投来的 CustomInboundMessage）是两份契约，所以是两条路由。
//
// 三条容易写反的规则，各自钉一条用例：
//   1. 信封 lane 与本进程 lane 不等**不是**错误 —— sidecar 在目标泳道不存在时会把请求
//      打回 prod，那正是本方案要的落回行为；
//   2. 缺 handed_off 标记要拒收 —— 那是自投循环的唯一阻断点；
//   3. 处理失败必须是非 2xx —— 投递方靠状态码判断这条消息有没有人处理。

import { afterAll, describe, expect, it } from 'bun:test';
import { Hono } from 'hono';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import { context } from '@middleware/context';

import { QQ_LANE_INBOUND_PATH, registerQqLaneInbound } from './lane-inbound';

const SECRET = 'inner-secret-under-test';
const originalSecret = process.env.INNER_HTTP_SECRET;
process.env.INNER_HTTP_SECRET = SECRET;
afterAll(() => {
    if (originalSecret === undefined) Reflect.deleteProperty(process.env, 'INNER_HTTP_SECRET');
    else process.env.INNER_HTTP_SECRET = originalSecret;
});

interface Handled {
    message: CustomInboundMessage;
    lane: string | undefined;
    botName: string | undefined;
    traceId: string;
}

function buildApp(options: { processLane?: string; fail?: Error } = {}) {
    const app = new Hono();
    const handled: Handled[] = [];
    registerQqLaneInbound(app, {
        processLane: () => options.processLane ?? 'prod',
        handle: async (message) => {
            handled.push({
                message,
                lane: context.getLane(),
                botName: context.getBotName(),
                traceId: context.getTraceId(),
            });
            if (options.fail) throw options.fail;
        },
    });
    return { app, handled };
}

function qqMessage(): Record<string, unknown> {
    return {
        botName: 'chiwei-qq',
        chatType: 'direct',
        conversationId: 'user_001',
        senderId: 'user_001',
        text: '你好',
        messageId: 'msg_10001',
        timestamp: '2026-06-27T10:00:00+08:00',
    };
}

function envelope(overrides: Record<string, unknown> = {}): Record<string, unknown> {
    return {
        channel: 'qq',
        event_type: 'qq.message.receive',
        global_message_id: 'cm-1',
        trace_id: 'trace-1',
        lane: 'ppe-foo',
        bot_name: 'chiwei-qq',
        handed_off: true,
        params: qqMessage(),
        ...overrides,
    };
}

async function post(
    app: Hono,
    body: unknown,
    auth: string | null = `Bearer ${SECRET}`,
): Promise<Response> {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (auth) headers.Authorization = auth;
    return app.request(QQ_LANE_INBOUND_PATH, { method: 'POST', headers, body: JSON.stringify(body) });
}

describe('QQ 泳道信封接收端', () => {
    it('没带 Authorization → 401，不进处理', async () => {
        const { app, handled } = buildApp();

        const res = await post(app, envelope(), null);

        expect(res.status).toBe(401);
        expect(handled.length).toBe(0);
    });

    it('Bearer token 不对 → 401，不进处理', async () => {
        const { app, handled } = buildApp();

        const res = await post(app, envelope(), 'Bearer wrong-secret');

        expect(res.status).toBe(401);
        expect(handled.length).toBe(0);
    });

    it('信封缺字段 → 400，不进处理', async () => {
        const { app, handled } = buildApp();

        const res = await post(app, envelope({ lane: undefined }));

        expect(res.status).toBe(400);
        expect(handled.length).toBe(0);
    });

    it('信封没有 handed_off 标记 → 400（自投循环的阻断点）', async () => {
        const { app, handled } = buildApp();

        const res = await post(app, envelope({ handed_off: undefined }));

        expect(res.status).toBe(400);
        expect((await res.json()).message).toContain('handed_off');
        expect(handled.length).toBe(0);
    });

    it('params 不是合法 CustomInboundMessage → 400，不进处理', async () => {
        const { app, handled } = buildApp();

        const res = await post(app, envelope({ params: { botName: 'chiwei-qq' } }));

        expect(res.status).toBe(400);
        expect(handled.length).toBe(0);
    });

    it('事件类型不被本服务认领 → 422，不进处理', async () => {
        const { app, handled } = buildApp();

        const res = await post(app, envelope({ event_type: 'im.message.receive_v1' }));

        expect(res.status).toBe(422);
        expect(handled.length).toBe(0);
    });

    it('渠道不是 qq → 422，不进处理', async () => {
        const { app, handled } = buildApp();

        const res = await post(app, envelope({ channel: 'lark' }));

        expect(res.status).toBe(422);
        expect(handled.length).toBe(0);
    });

    it('处理抛错 → 500（投递方据此判定这条消息没人处理）', async () => {
        const { app, handled } = buildApp({ fail: new Error('projection lock timeout') });

        const res = await post(app, envelope());

        expect(res.status).toBe(500);
        expect(handled.length).toBe(1);
    });

    it('信封 lane 与本进程 lane 不一致仍然处理，并用信封的 lane 建立 context', async () => {
        const { app, handled } = buildApp({ processLane: 'prod' });

        const res = await post(app, envelope({ lane: 'ppe-foo' }));

        expect(res.status).toBe(200);
        expect(handled.length).toBe(1);
        expect(handled[0].lane).toBe('ppe-foo');
        expect(handled[0].botName).toBe('chiwei-qq');
        expect(handled[0].traceId).toBe('trace-1');
        expect(handled[0].message.messageId).toBe('msg_10001');
    });

    it('2xx 回报实际处理这条消息的进程 lane，投递方据此区分送达泳道与落回 prod', async () => {
        const inLane = buildApp({ processLane: 'ppe-foo' });
        const fellBack = buildApp({ processLane: 'prod' });

        const delivered = await post(inLane.app, envelope());
        const fallback = await post(fellBack.app, envelope());

        expect(await delivered.json()).toEqual({ success: true, handled_by_lane: 'ppe-foo' });
        expect(await fallback.json()).toEqual({ success: true, handled_by_lane: 'prod' });
    });
});
