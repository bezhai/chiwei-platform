// 交接的接收侧。投递方（prod 的本服务）打的是这个端点，sidecar 在泳道 Service 不存在
// 时会把它原样打回 prod 自己 —— 所以这里的每一条断言都要同时想清楚"泳道在"和"泳道
// 不在"两种情形。

import { afterAll, beforeAll, describe, expect, it } from 'bun:test';
import { Hono } from 'hono';
import { errorHandler } from '@inner/shared/middleware';

import { UnprocessableLarkEvent, type LarkEvent } from './lark-event';
import type { InboundLaneEnvelope } from './lane-envelope';
import { LANE_INBOUND_PATH, registerLarkLaneInbound } from './lane-inbound';

const SECRET = 'inner-secret-for-tests';
const originalSecret = process.env.INNER_HTTP_SECRET;

beforeAll(() => {
    process.env.INNER_HTTP_SECRET = SECRET;
});

afterAll(() => {
    if (originalSecret === undefined) Reflect.deleteProperty(process.env, 'INNER_HTTP_SECRET');
    else process.env.INNER_HTTP_SECRET = originalSecret;
});

function envelope(overrides: Partial<InboundLaneEnvelope> = {}): InboundLaneEnvelope {
    return {
        channel: 'lark',
        event_type: 'im.message.receive_v1',
        global_message_id: 'cm_1',
        trace_id: 'trace-1',
        lane: 'ppe-x',
        bot_name: 'chiwei',
        params: { message: { message_id: 'om_1' } },
        handed_off: true,
        ...overrides,
    };
}

function build(
    options: {
        lane?: string;
        deliver?: (event: LarkEvent) => Promise<void>;
        handles?: (eventType: string) => boolean;
    } = {},
) {
    const delivered: LarkEvent[] = [];
    const app = new Hono();
    // 真实装配里错误处理由 createLarkServiceApp 挂上，端点自己不吞错。
    app.onError(errorHandler);
    registerLarkLaneInbound(app, {
        lane: options.lane ?? 'ppe-x',
        handles: options.handles ?? ((type) => type === 'im.message.receive_v1'),
        deliver:
            options.deliver ??
            (async (event) => {
                delivered.push(event);
            }),
    });

    const post = (body: unknown, headers: Record<string, string> = {}) =>
        app.request(LANE_INBOUND_PATH, {
            method: 'POST',
            headers: {
                Authorization: `Bearer ${SECRET}`,
                'Content-Type': 'application/json',
                ...headers,
            },
            body: typeof body === 'string' ? body : JSON.stringify(body),
        });

    return { app, delivered, post };
}

// 端点路径是**跨服务契约**的一半（另一半是投递方的 fetch）。改名只改一边的症状是
// 投递方拿 404，而 404 会被当成投递失败 —— 消息就此没人处理。
describe('LANE_INBOUND_PATH', () => {
    it('is the internal path the handoff sender posts to', () => {
        expect(LANE_INBOUND_PATH).toBe('/api/internal/lark/lane-inbound');
    });
});

describe('鉴权', () => {
    it('没有 Authorization 头就拒绝', async () => {
        const { app, delivered } = build();
        const res = await app.request(LANE_INBOUND_PATH, {
            method: 'POST',
            body: JSON.stringify(envelope()),
        });

        expect(res.status).toBe(401);
        expect(delivered).toEqual([]);
    });

    it('口令不对就拒绝', async () => {
        const { delivered, post } = build();
        const res = await post(envelope(), { Authorization: 'Bearer wrong' });

        expect(res.status).toBe(401);
        expect(delivered).toEqual([]);
    });
});

describe('信封边界', () => {
    it('不是 JSON 的请求体拒绝', async () => {
        const { delivered, post } = build();
        const res = await post('not json at all');

        expect(res.status).toBe(400);
        expect(delivered).toEqual([]);
    });

    // 必填字段集合与 channel-server 的信封端点逐字对齐。线协议声称两侧是镜像，少验一个
    // 字段就不是镜像了 —— 缺 trace_id 的信封在这一侧被收下、在那一侧被拒，同一次跨渠道
    // 排查会得到两种结论。
    it.each(['channel', 'event_type', 'global_message_id', 'trace_id', 'lane', 'bot_name'] as const)(
        '缺 %s 的信封拒绝',
        async (field) => {
            const broken = envelope();
            delete (broken as unknown as Record<string, unknown>)[field];
            const { delivered, post } = build();

            const res = await post(broken);

            expect(res.status).toBe(400);
            expect(delivered).toEqual([]);
        },
    );

    it('没有事件载荷的信封拒绝', async () => {
        const { delivered, post } = build();
        const res = await post(envelope({ params: undefined }));

        expect(res.status).toBe(400);
        expect(delivered).toEqual([]);
    });

    // 这一条不是形式校验，是安全边界：sidecar 在泳道缺席时把请求打回 prod 自己，没有
    // 「已交接」标记的信封会被重新判定、重新投递，无限自投。
    it('没带「已交接」标记的信封拒绝', async () => {
        const { delivered, post } = build();
        const res = await post(envelope({ handed_off: undefined }));

        expect(res.status).toBe(400);
        expect(delivered).toEqual([]);
    });

    // 报文成立、只是装的不是本服务认领的东西 —— 与"报文本身不成立"分开用 422 表达，
    // 口径与 channel-server 的信封端点一致。
    it('本服务不认领的事件类型拒绝', async () => {
        const { delivered, post } = build();
        const res = await post(envelope({ event_type: 'im.chat.updated_v1' }));

        expect(res.status).toBe(422);
        expect(delivered).toEqual([]);
    });

    it('渠道不是 lark 的信封拒绝', async () => {
        const { delivered, post } = build();
        const res = await post(envelope({ channel: 'qq' }));

        expect(res.status).toBe(422);
        expect(delivered).toEqual([]);
    });
});

describe('处理', () => {
    it('按信封重建事件，lane 与「已交接」都从信封上来', async () => {
        const { delivered, post } = build();

        const res = await post(envelope());

        expect(res.status).toBe(200);
        expect(delivered).toEqual([
            {
                type: 'im.message.receive_v1',
                payload: { message: { message_id: 'om_1' } },
                botName: 'chiwei',
                traceId: 'trace-1',
                lane: 'ppe-x',
                handedOff: true,
            },
        ]);
    });

    // 落回 prod 的情形：信封说 ppe-x，接住它的是 prod 进程。旧的归属校验会在这里拒绝，
    // 而拒绝就等于泳道没部署时消息谁也不处理。
    it('信封的泳道与本进程不同时照常处理，用信封的泳道建立上下文', async () => {
        const { delivered, post } = build({ lane: 'prod' });

        const res = await post(envelope({ lane: 'ppe-x' }));

        expect(res.status).toBe(200);
        expect(delivered).toHaveLength(1);
        expect(delivered[0]!.lane).toBe('ppe-x');
    });

    // 投递方靠这个字段区分「送达泳道」与「落回 prod」—— 从投递结果上这两者一模一样。
    it('回报是哪条泳道的进程处理的', async () => {
        const onLane = await build({ lane: 'ppe-x' }).post(envelope());
        expect(await onLane.json()).toMatchObject({ handled_by_lane: 'ppe-x' });

        const fellBack = await build({ lane: 'prod' }).post(envelope());
        expect(await fellBack.json()).toMatchObject({ handled_by_lane: 'prod' });
    });

    // 投递方不重试，2xx 是"这条消息已经被处理完了"的唯一凭据。先应答再异步处理的话，
    // 处理失败就再也没人知道。
    it('处理完成之前不应答', async () => {
        let release: (() => void) | undefined;
        const handled = new Promise<void>((resolve) => {
            release = resolve;
        });
        const { post } = build({ deliver: () => handled });

        let answered = false;
        const responded = Promise.resolve(post(envelope())).then((res) => {
            answered = true;
            return res;
        });
        await Bun.sleep(2);
        expect(answered).toBe(false);

        release!();
        expect((await responded).status).toBe(200);
    });

    it('处理抛错时应答非 2xx，不当成收下了', async () => {
        const { post } = build({
            deliver: async () => {
                throw new Error('postgres is down');
            },
        });

        const res = await post(envelope());

        expect(res.ok).toBe(false);
        expect(res.status).toBe(500);
    });

    // 载荷本身没救（比如消息事件里没有 message_id）也一样是非 2xx：投递方不重试，
    // 把它算成成功就是一条静默丢失。
    it('载荷永久处理不了时也应答非 2xx', async () => {
        const { post } = build({
            deliver: async () => {
                throw new UnprocessableLarkEvent('event carries no message id');
            },
        });

        expect((await post(envelope())).ok).toBe(false);
    });
});
