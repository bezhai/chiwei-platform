// 三个入口，一层解析。本文件是这句话的判据：webhook、长连、泳道交接的 HTTP 各走各的
// 传输，但同一条飞书消息经过它们之后，交到下游手里的领域对象必须一模一样。

import { createCipheriv, createHash, randomBytes } from 'node:crypto';
import { afterAll, beforeAll, describe, expect, it } from 'bun:test';
import { Hono } from 'hono';
import { context } from '@inner/shared/middleware';
import type { BotConfig } from '@inner/shared/entities';
import type { EventDispatcher } from '@larksuiteoapi/node-sdk';

import { createLarkInbound, type LarkInboundPorts } from './inbound';
import type { LarkEvent } from './ingress/lark-event';
import { LANE_INBOUND_PATH } from './ingress/lane-inbound';
import type { InboundLaneEnvelope } from './ingress/lane-envelope';
import type { LarkWebSocketClient } from './ingress/websocket';
import type { LarkMessageReading } from './message/read-message-event';
import type { LarkRecallEvent } from './message/wire';

const INNER_SECRET = 'inner-secret-for-tests';
const originalSecret = process.env.INNER_HTTP_SECRET;

beforeAll(() => {
    process.env.INNER_HTTP_SECRET = INNER_SECRET;
});

afterAll(() => {
    if (originalSecret === undefined) Reflect.deleteProperty(process.env, 'INNER_HTTP_SECRET');
    else process.env.INNER_HTTP_SECRET = originalSecret;
});

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

interface Pressed {
    payload: unknown;
    botName: string;
}

interface Recalled {
    recall: LarkRecallEvent;
    receivedAt: Date;
}

function build(bots: BotConfig[] = [bot()]) {
    const seen: Seen[] = [];
    const pressed: Pressed[] = [];
    const recalled: Recalled[] = [];
    const recorded: unknown[] = [];
    const ports: LarkInboundPorts = {
        roster: { getAllBotConfigs: () => bots },
        lane: 'ppe-x',
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
        onCardAction: async (payload) => {
            pressed.push({ payload, botName: context.getBotName() });
        },
        onRecall: async (recall, receivedAt) => void recalled.push({ recall, receivedAt }),
    };
    return { inbound: createLarkInbound(ports), seen, pressed, recalled, recorded };
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

/** 一次卡片交互。走的是 /webhook/{bot}/card 那条路，报文里没有 event_type。 */
const CARD_ACTION = {
    action: { tag: 'button', value: { type: 'update-photo-card', tags: ['刻晴'] } },
    context: { open_message_id: 'om_card', open_chat_id: 'oc_1' },
    operator: { open_id: 'ou_presser', union_id: 'on_presser', user_id: 'u1' },
    token: 'card-token',
};

async function throughCardWebhook() {
    const built = build();
    const app = new Hono();
    built.inbound.registerWebhooks(app);
    const request = asLarkSends(CARD_ACTION);
    const res = await app.request('/webhook/chiwei/card', {
        method: 'POST',
        headers: request.headers,
        body: request.body,
    });
    await Bun.sleep(2);
    return { ...built, status: res.status };
}

/**
 * 一次真人撤回。跟消息事件走同一条 webhook，但报文里只有几个可选字段 —— 没有发送者、
 * 没有正文，也**没有撤回者的身份**（只有一个角色枚举）。
 */
const LARK_RECALL = {
    message_id: 'om_1',
    chat_id: 'oc_1',
    recall_time: '1757573454',
    recall_type: 'message_owner',
};

function recallBody() {
    return {
        schema: '2.0',
        header: {
            event_id: 'e2',
            token: 'vtok',
            create_time: '1700000000000',
            event_type: 'im.message.recalled_v1',
            app_id: 'cli_chiwei',
        },
        event: LARK_RECALL,
    };
}

async function throughRecallWebhook() {
    const built = build();
    const app = new Hono();
    built.inbound.registerWebhooks(app);
    const request = asLarkSends(recallBody());
    const res = await app.request('/webhook/chiwei/event', {
        method: 'POST',
        headers: request.headers,
        body: request.body,
    });
    await Bun.sleep(2);
    return { ...built, status: res.status };
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
        handed_off: true,
        ...overrides,
    };
}

/** 入口三：prod 交接过来的一次内部 HTTP。 */
async function throughLaneHttp(env: unknown = laneEnvelope()) {
    const built = build();
    const app = new Hono();
    built.inbound.registerLaneInbound(app);

    const res = await app.request(LANE_INBOUND_PATH, {
        method: 'POST',
        headers: {
            Authorization: `Bearer ${INNER_SECRET}`,
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(env),
    });
    return { ...built, status: res.status };
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

    it('parses a lane envelope handed over by HTTP', async () => {
        const { seen, status } = await throughLaneHttp();
        expect(status).toBe(200);
        expect(seen).toHaveLength(1);
        expect(seen[0]!.reading.message.messageId).toBe('om_1');
    });

    // 这条是本段的核心：三个入口不是三条解析路径。
    it('produces the very same domain object no matter which entry it came through', async () => {
        const webhook = (await throughWebhook()).seen[0]!.reading;
        const websocket = (await throughWebSocket()).seen[0]!.reading;
        const laneHttp = (await throughLaneHttp()).seen[0]!.reading;

        expect(websocket.content).toEqual(webhook.content);
        expect(laneHttp.content).toEqual(webhook.content);
        expect(websocket.inbound).toEqual(webhook.inbound);
        expect(laneHttp.inbound).toEqual(webhook.inbound);
    });

    // 交接来的信封带着它自己的泳道，接收进程是 prod 还是泳道都不改写它 —— 落回 prod
    // 时靠这一条，下游才会继续投泳道的队列。
    it('carries the lane out of a handed-off envelope even on a prod process', async () => {
        const { seen } = await throughLaneHttp(laneEnvelope({ lane: 'ppe-other' }));
        expect(seen[0]!.lane).toBe('ppe-other');
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
        expect((await throughLaneHttp()).seen[0]!.lane).toBe('ppe-x');
        expect((await throughWebhook()).seen[0]!.lane).toBe('');
    });

    it('names the handling bot on every entry', async () => {
        expect((await throughWebhook()).seen[0]!.botName).toBe('chiwei');
        expect((await throughWebSocket()).seen[0]!.botName).toBe('chiwei');
        expect((await throughLaneHttp()).seen[0]!.botName).toBe('chiwei');
    });

    // 审计记的是"飞书发生了一件事"。泳道信封是重放，那件事在它第一次进来时已经记过
    // 了，再记一遍会让同一条事件在审计里出现两次。
    it('records the raw event from Lark but not a lane replay', async () => {
        expect((await throughWebhook()).recorded).toHaveLength(1);
        expect((await throughWebSocket()).recorded).toHaveLength(1);
        expect((await throughLaneHttp()).recorded).toEqual([]);
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

    // 卡片回调是**第二条入站路径**：它不过解析层、不过规则引擎，报文里也没有
    // event_type（类型由 /webhook/{bot}/card 这个入口本身决定）。它一度整条断着 ——
    // 路由注册了、事件槽也标了类型，处理表里却没有它，于是回调进来只打一条
    // "nobody handles" 的 warn 就没了。
    it('hands a card action to whoever claims card actions', async () => {
        const { pressed, seen, status } = await throughCardWebhook();

        expect(status).toBe(200);
        expect(seen).toEqual([]);
        expect(pressed).toHaveLength(1);
        expect(pressed[0]!.payload).toMatchObject({
            token: 'card-token',
            action: { value: { type: 'update-photo-card', tags: ['刻晴'] } },
        });
    });

    // 卡片回调也要知道是替哪个 bot 接的：飞书客户端按它选池子，选错就是另一个人设
    // 去更新这张卡片。
    it('names the handling bot on the card route too', async () => {
        expect((await throughCardWebhook()).pressed[0]!.botName).toBe('chiwei');
    });

    it('records the raw card payload for audit, same as any other event', async () => {
        expect((await throughCardWebhook()).recorded).toHaveLength(1);
    });

    // 撤回是**第三条入站路径**：跟卡片回调一样不过解析层（报文里没有发送者、没有正文，
    // 也不会进 common_message 建新行），它要做的只是把已经在库里的那一行标成撤回。
    describe('撤回事件', () => {
        it('把撤回报文交给认领它的人', async () => {
            const { recalled, status } = await throughRecallWebhook();

            expect(status).toBe(200);
            expect(recalled).toHaveLength(1);
            expect(recalled[0]!.recall).toMatchObject({
                message_id: 'om_1',
                chat_id: 'oc_1',
                recall_time: '1757573454',
                recall_type: 'message_owner',
            });
        });

        // 库里落的撤回时刻**永远**是这个值——报文里那个 recall_time 的单位没有实证
        // 样本可裁决，所以一个字都不解析（理由写在 recall-message.ts 文件头）。入口是
        // 先应答、再异步处理，两者之间隔多久没有保证，所以它必须在应答那一刻就取好。
        it('把应答那一刻的时间一起交出去，那就是落库的撤回时刻', async () => {
            const before = Date.now();
            const { recalled } = await throughRecallWebhook();
            const after = Date.now();

            expect(recalled[0]!.receivedAt).toBeInstanceOf(Date);
            expect(recalled[0]!.receivedAt.getTime()).toBeGreaterThanOrEqual(before);
            expect(recalled[0]!.receivedAt.getTime()).toBeLessThanOrEqual(after);
        });

        // **spec 决策 5：撤回事件不走泳道交接。** 交接的判定与投递整个长在消息投影里
        // （projectLarkInbound 算出目标泳道、拼信封、打那一次内部 HTTP），而撤回事件
        // 连解析层都不过，更进不了投影。所以"发送侧不会交接撤回事件"在这一层的判据
        // 就是：它一次都没有走上消息那条路。
        it('不走消息那条路，因此发送侧永远不会把它交接给泳道', async () => {
            const { seen, recalled } = await throughRecallWebhook();

            expect(seen).toEqual([]);
            expect(recalled).toHaveLength(1);
        });

        // 认领面直接取处理表的键，所以认领了撤回之后，交接端点也跟着接受这种信封 ——
        // 一个行为变化，登记在这里。实际上不会有人投：发送侧不产生撤回信封（上一条）。
        // 真被投进来（比如以后有人开了那条路）也是对的：处理器只按 om_id 定位、只写
        // recalled_at，跑在哪条泳道上都是同一件事。
        it('认领之后，交接端点也不再拒收撤回信封', async () => {
            const { recalled, status } = await throughLaneHttp(
                laneEnvelope({ event_type: 'im.message.recalled_v1', params: LARK_RECALL }),
            );

            expect(status).toBe(200);
            expect(recalled).toHaveLength(1);
        });

        it('审计照记，跟别的飞书事件一样', async () => {
            expect((await throughRecallWebhook()).recorded).toHaveLength(1);
        });
    });

    // 群成员变化这些还没人认领。认领面直接取处理表的键，所以装配层不会跟处理表脱节；
    // 应答成功就是静默丢失，交接端点因此明着拒绝。422 而不是 400：报文本身是成立的，
    // 只是装的东西本服务不认领（与 channel-server 的镜像端点同一口径）。
    it('refuses an event type nobody claims yet', async () => {
        const { seen, status } = await throughLaneHttp(
            laneEnvelope({ event_type: 'im.chat.updated_v1', params: { chat_id: 'oc_1' } }),
        );
        expect(status).toBe(422);
        expect(seen).toEqual([]);
    });

    // 载荷根本不是一条消息（没有 message_id）：解析层交不出东西，重发也一样。投递方
    // 不重试，所以只能靠非 2xx 让它知道这条消息没人处理。
    it('answers non-2xx for a payload that is not a message', async () => {
        const { seen, status } = await throughLaneHttp(laneEnvelope({ params: { sender: {} } }));
        expect(status).toBeGreaterThanOrEqual(500);
        expect(seen).toEqual([]);
    });
});
