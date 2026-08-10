// 三个入口，一层解析。本文件是这句话的判据：webhook、长连、泳道队列各走各的传输，
// 但同一条飞书消息经过它们之后，交到下游手里的领域对象必须一模一样。

import { createCipheriv, createHash, randomBytes } from 'node:crypto';
import { describe, expect, it } from 'bun:test';
import { Hono } from 'hono';
import { context } from '@inner/shared/middleware';
import type { BotConfig } from '@inner/shared/entities';
import type { EventDispatcher } from '@larksuiteoapi/node-sdk';

import { createLarkInbound, type LarkInboundPorts } from './inbound';
import type { LarkEvent } from './ingress/lark-event';
import type { LaneClaim } from './ingress/lane-claim';
import type { InboundLaneEnvelope, LaneChannel } from './ingress/lane-queue';
import type { LarkWebSocketClient } from './ingress/websocket';
import type { LarkMessageReading } from './message/read-message-event';

// 生产上每个飞书应用都开了消息加密（larkCredentials 也要求 encrypt_key 非空），
// 所以这里走的是真实形态：报文加密、由 SDK 解密。
const ENCRYPT_KEY = 'test-encrypt-key';

const credentials = {
    app_id: 'cli_chiwei',
    app_secret: 's',
    encrypt_key: ENCRYPT_KEY,
    verification_token: 'vtok',
    robot_union_id: 'on_chiwei',
};

/**
 * 按飞书真实的 webhook 投递方式打包一次请求：
 *   - 报文用 AES-256-CBC 加密（密钥是 sha256(encrypt_key)，IV 前置，整体 base64）
 *   - 请求头带签名 sha256(timestamp + nonce + encrypt_key + 请求体)
 * SDK 两样都会验，少一样就是 "verification failed event"。
 */
function asLarkSends(payload: unknown): { body: string; headers: Record<string, string> } {
    const key = createHash('sha256').update(ENCRYPT_KEY).digest();
    const iv = randomBytes(16);
    const cipher = createCipheriv('aes-256-cbc', key, iv);
    const encrypted = Buffer.concat([
        cipher.update(JSON.stringify(payload), 'utf8'),
        cipher.final(),
    ]);

    const body = JSON.stringify({ encrypt: Buffer.concat([iv, encrypted]).toString('base64') });
    const timestamp = '1700000000';
    const nonce = 'nonce-1';
    const signature = createHash('sha256')
        .update(timestamp + nonce + ENCRYPT_KEY + body)
        .digest('hex');

    return {
        body,
        headers: {
            'content-type': 'application/json',
            'x-lark-request-timestamp': timestamp,
            'x-lark-request-nonce': nonce,
            'x-lark-signature': signature,
        },
    };
}

function bot(overrides: Partial<BotConfig> = {}): BotConfig {
    return {
        bot_name: 'chiwei',
        channel: 'lark',
        common_user_id: 'cu_chiwei',
        persona_id: 'p_chiwei',
        init_type: 'http',
        credentials,
        ...overrides,
    } as BotConfig;
}

interface Seen {
    reading: LarkMessageReading;
    event: LarkEvent;
    lane: string;
    botName: string;
}

function build(bots: BotConfig[] = [bot()]) {
    const seen: Seen[] = [];
    const recorded: unknown[] = [];
    const ports: LarkInboundPorts = {
        roster: { getAllBotConfigs: () => bots },
        personaName: (personaId) => (personaId === 'p_chiwei' ? '赤尾' : null),
        record: async (payload) => void recorded.push(payload),
        onMessage: async (reading, event) => {
            seen.push({
                reading,
                event,
                lane: context.getLane(),
                botName: context.getBotName(),
            });
        },
    };
    return { inbound: createLarkInbound(ports), seen, recorded };
}

// 同一条飞书消息，三种信封各包一遍。
const LARK_MESSAGE = {
    sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
    message: {
        message_id: 'om_1',
        chat_id: 'oc_1',
        chat_type: 'group',
        create_time: '1700000000000',
        message_type: 'text',
        content: '{"text":"hi @_user_1"}',
        mentions: [
            {
                key: '@_user_1',
                id: { union_id: 'on_chiwei' },
                name: 'chiwei-raw',
                mentioned_type: 'bot',
                bot_info: { app_id: 'cli_chiwei' },
            },
        ],
    },
};

function webhookBody() {
    return {
        schema: '2.0',
        header: {
            event_id: 'e1',
            token: 'vtok',
            create_time: '1700000000000',
            event_type: 'im.message.receive_v1',
            app_id: 'cli_chiwei',
        },
        event: LARK_MESSAGE,
    };
}

function laneEnvelope(overrides: Partial<InboundLaneEnvelope> = {}): InboundLaneEnvelope {
    return {
        channel: 'lark',
        event_type: 'im.message.receive_v1',
        global_message_id: 'cm_1',
        trace_id: 'trace-1',
        lane: 'ppe-x',
        bot_name: 'chiwei',
        params: { ...LARK_MESSAGE, app_id: 'cli_chiwei', event_type: 'im.message.receive_v1' },
        ...overrides,
    };
}

async function throughWebhook(bots?: BotConfig[]) {
    const built = build(bots);
    const app = new Hono();
    built.inbound.registerWebhooks(app);
    const request = asLarkSends(webhookBody());
    const res = await app.request('/webhook/chiwei/event', {
        method: 'POST',
        headers: request.headers,
        body: request.body,
    });
    await Bun.sleep(2);
    return { ...built, status: res.status };
}

async function throughWebSocket() {
    const built = build([bot({ init_type: 'websocket' })]);
    let dispatcher: EventDispatcher | undefined;
    const client: LarkWebSocketClient = {
        start: async (params) => {
            dispatcher = params.eventDispatcher;
        },
        close: () => {},
        state: () => 'connected',
    };

    await built.inbound.openWebSockets(() => client);
    // 长连帧不带 HTTP 签名头，SDK 的 WSClient 是用 needCheck:false 调 dispatcher 的
    // （连接本身已经过鉴权）。这里照它的调法来。
    await (
        dispatcher as unknown as {
            invoke(d: unknown, p: { needCheck: boolean }): Promise<unknown>;
        }
    ).invoke(Object.assign(Object.create({ headers: {} }), webhookBody()), { needCheck: false });
    await Bun.sleep(2);
    return built;
}

function laneChannel() {
    const consumers = new Map<
        string,
        (msg: { content: Buffer; fields?: { redelivered?: boolean } } | null) => Promise<void>
    >();
    const acked: unknown[] = [];
    const nacked: Array<{ requeue: boolean }> = [];
    const amqp: LaneChannel = {
        assertQueue: async () => {},
        prefetch: async () => {},
        consume: async (queue, handler) => {
            consumers.set(queue, handler);
            return {};
        },
        ack: (msg) => void acked.push(msg),
        nack: (_msg, _allUpTo, requeue) => void nacked.push({ requeue }),
    };
    return {
        amqp,
        acked,
        nacked,
        subscribed: () => [...consumers.keys()],
        push: async (env: unknown, queue = 'inbound_lane.ppe-x') => {
            await consumers.get(queue)!({ content: Buffer.from(JSON.stringify(env)) });
        },
    };
}

async function throughLane(
    env: unknown = laneEnvelope(),
    options: { channelQueue?: boolean; queue?: string } = {},
) {
    const built = build();
    const mq = laneChannel();
    const keys = new Map<string, 'in-flight' | 'done'>();
    await built.inbound.consumeLane('ppe-x', {
        amqp: mq.amqp,
        store: {
            claim: async (k): Promise<LaneClaim> => {
                const held = keys.get(k);
                if (held) return held;
                keys.set(k, 'in-flight');
                return 'claimed';
            },
            complete: async (k) => void keys.set(k, 'done'),
            release: async (k) => void keys.delete(k),
        },
        wait: async () => {},
        // 开关默认关，测试也显式注入：默认实现会去 paas-engine 拉一次动态配置。
        channelQueueEnabled: async () => options.channelQueue === true,
    });
    await mq.push(env, options.queue);
    return { ...built, acked: mq.acked, nacked: mq.nacked, subscribed: mq.subscribed() };
}

describe('createLarkInbound', () => {
    it('parses a webhook event', async () => {
        const { seen, status } = await throughWebhook();
        expect(status).toBe(200);
        expect(seen).toHaveLength(1);
        expect(seen[0]!.reading.message.messageId).toBe('om_1');
    });

    it('parses a long-connection event', async () => {
        const { seen } = await throughWebSocket();
        expect(seen).toHaveLength(1);
        expect(seen[0]!.reading.message.messageId).toBe('om_1');
    });

    it('parses a lane envelope', async () => {
        const { seen, acked } = await throughLane();
        expect(seen).toHaveLength(1);
        expect(seen[0]!.reading.message.messageId).toBe('om_1');
        expect(acked).toHaveLength(1);
    });

    // 这条是本段的核心：三个入口不是三条解析路径。
    it('produces the very same domain object no matter which entry it came through', async () => {
        const webhook = (await throughWebhook()).seen[0]!.reading;
        const websocket = (await throughWebSocket()).seen[0]!.reading;
        const lane = (await throughLane()).seen[0]!.reading;

        expect(websocket.content).toEqual(webhook.content);
        expect(lane.content).toEqual(webhook.content);
        expect(websocket.inbound).toEqual(webhook.inbound);
        expect(lane.inbound).toEqual(webhook.inbound);
    });

    it('resolves a mention of one of our bots to its persona name', async () => {
        const { seen } = await throughWebhook();
        expect(seen[0]!.reading.content).toEqual([
            { type: 'text', value: 'hi ' },
            {
                type: 'mention',
                value: '赤尾',
                meta: { channel_user_id: 'on_chiwei', bot_common_user_id: 'cu_chiwei' },
            },
        ]);
    });

    // 泳道信封是**另一个进程判定过**的重放，它自己带着该在哪条泳道处理。飞书直连的
    // 两个入口没有这个概念，走的是本进程自己的泳道。
    it('carries the lane out of the envelope, and only out of the envelope', async () => {
        expect((await throughLane()).seen[0]!.lane).toBe('ppe-x');
        expect((await throughWebhook()).seen[0]!.lane).toBe('');
    });

    it('names the handling bot on every entry', async () => {
        expect((await throughWebhook()).seen[0]!.botName).toBe('chiwei');
        expect((await throughWebSocket()).seen[0]!.botName).toBe('chiwei');
        expect((await throughLane()).seen[0]!.botName).toBe('chiwei');
    });

    // 审计记的是"飞书发生了一件事"。泳道信封是重放，那件事在它第一次进来时已经记过
    // 了，再记一遍会让同一条事件在审计里出现两次。
    it('records the raw event from Lark but not a lane replay', async () => {
        expect((await throughWebhook()).recorded).toHaveLength(1);
        expect((await throughWebSocket()).recorded).toHaveLength(1);
        expect((await throughLane()).recorded).toEqual([]);
    });

    it('routes webhooks only for bots that receive over HTTP', async () => {
        const built = build([bot({ init_type: 'websocket' })]);
        const app = new Hono();
        built.inbound.registerWebhooks(app);

        const request = asLarkSends(webhookBody());
        const res = await app.request('/webhook/chiwei/event', {
            method: 'POST',
            headers: request.headers,
            body: request.body,
        });
        expect(res.status).toBe(404);
    });

    it('opens long connections only for bots that receive over one', async () => {
        const built = build([bot({ init_type: 'http' })]);
        const opened: string[] = [];
        await built.inbound.openWebSockets((creds) => {
            opened.push(creds.app_id);
            return { start: async () => {}, close: () => {}, state: () => 'connected' };
        });
        expect(opened).toEqual([]);
    });

    // 群成员变化、卡片回调这些还没人认领。ACK 掉就是静默丢失，所以队列那条路要
    // 明着拒绝。
    it('refuses to acknowledge an event type nobody claims yet', async () => {
        const { seen, acked, nacked } = await throughLane(
            laneEnvelope({ event_type: 'im.chat.updated_v1', params: { chat_id: 'oc_1' } }),
        );
        expect(seen).toEqual([]);
        expect(acked).toEqual([]);
        expect(nacked).toEqual([{ requeue: false }]);
    });

    // 载荷根本不是一条消息（没有 message_id）：解析层交不出东西，重投也一样。以前
    // 这里会被当成"处理成功"ACK 掉。
    it('refuses to acknowledge a payload that is not a message', async () => {
        const { seen, acked, nacked } = await throughLane(laneEnvelope({ params: { sender: {} } }));
        expect(seen).toEqual([]);
        expect(acked).toEqual([]);
        expect(nacked).toEqual([{ requeue: false }]);
    });

    // 分区前的队列同时装着 QQ 和飞书的信封，两个服务竞争消费。抢到 QQ 的那一条时 ACK
    // 就是把它吃掉 —— 对面永远收不到。
    it('hands a QQ envelope back instead of eating it', async () => {
        const { seen, acked, nacked } = await throughLane(laneEnvelope({ channel: 'qq' }));
        expect(seen).toEqual([]);
        expect(acked).toEqual([]);
        expect(nacked).toEqual([{ requeue: true }]);
    });

    // 分区之后队列名从本服务的 channel 拼出来，装配层不该另外配一个"我是谁"。
    it('subscribes the partitioned queue under its own channel', async () => {
        const { subscribed } = await throughLane(laneEnvelope(), { channelQueue: true });
        expect(subscribed).toEqual(['inbound_lane.ppe-x', 'inbound_lane.lark.ppe-x']);
    });

    it('parses a lane envelope off the partitioned queue exactly the same way', async () => {
        const { seen, acked } = await throughLane(laneEnvelope(), {
            channelQueue: true,
            queue: 'inbound_lane.lark.ppe-x',
        });
        expect(seen).toHaveLength(1);
        expect(seen[0]!.reading.message.messageId).toBe('om_1');
        expect(acked).toHaveLength(1);
    });
});
