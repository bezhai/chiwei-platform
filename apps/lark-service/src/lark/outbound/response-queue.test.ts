// 出站队列这一头：订阅哪条队列、什么时候才订阅、拿到不属于自己的消息怎么办、
// 什么时候 ACK。
//
// 队列名不在这里写字面量，而是接到 contracts/mq-channel-routes.json 那份**跨语言
// 契约向量**上 —— 生产者是 agent-service（Python），消费者是本服务（TS），两边各写
// 各的 expected 时，「实现和本地 expected 一起被改」或「CI 只跑了一侧」都会同时变绿，
// 而失效的表现是两个服务静默守着对方不知道的队列：没人消费，或者两个人都消费。

import { describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ConsumeMessage } from 'amqplib';
import type { Route } from '@inner/shared/mq';

import type { LarkChatResponse } from './chat-response';
import { deliverLarkChatResponse, type LarkDeliveryDeps } from './deliver';
import type { PostContent } from './post-content';
import {
    larkChatResponseBinding,
    larkChatResponseQueue,
    larkChatResponseRoute,
    type LarkResponseChannel,
    type LarkResponseConsumerDeps,
} from './response-queue';
import { LarkOutboundSubscriptions, type OutboundSubscriptionPort } from './subscription';

// ---------------------------------------------------------------------------
// 跨语言契约向量
// ---------------------------------------------------------------------------

interface ContractCase {
    name: string;
    base: string;
    channel: string;
    lane: string | null;
    expect: { queue: string; rk: string; queue_args: Record<string, unknown> };
}

interface ChannelRouteContract {
    base_routes: Record<string, { queue: string; rk: string }>;
    cases: ContractCase[];
}

// Python 侧读的是同一份：apps/agent-service/tests/unit/infra/test_channel_routes.py
// TS 共享包侧：packages/ts-shared/src/mq/channel-route.test.ts
const CONTRACT_PATH = resolve(import.meta.dir, '../../../../../contracts/mq-channel-routes.json');
const contract = JSON.parse(readFileSync(CONTRACT_PATH, 'utf8')) as ChannelRouteContract;

/** 契约里跟本服务有关的那几条：base=chat_response 且 channel=lark。 */
const LARK_RESPONSE_CASES = contract.cases.filter(
    (c) => c.base === 'chat_response' && c.channel === 'lark',
);

describe('订阅哪条队列 — 接在跨语言契约向量上', () => {
    it('契约里确实有飞书的 chat_response（向量缩水要立刻暴露）', () => {
        expect(LARK_RESPONSE_CASES.length).toBeGreaterThan(0);
        // prod 和泳道两种都要有，否则下面的用例只覆盖了一半。
        expect(LARK_RESPONSE_CASES.some((c) => c.lane === null)).toBe(true);
        expect(LARK_RESPONSE_CASES.some((c) => c.lane !== null)).toBe(true);
    });

    for (const c of LARK_RESPONSE_CASES) {
        it(`${c.name}：本服务算出来的队列名与 rk 逐值命中`, () => {
            expect(larkChatResponseQueue(c.lane ?? undefined)).toBe(c.expect.queue);
            // rk 由 declareRoute 拿去绑队列，错了等于绑到别人的流量上。
            expect(larkChatResponseRoute().rk).toBe(
                contract.base_routes.chat_response!.rk + '.lark',
            );
        });
    }

    it('声明用的 Route 就是队列名的来源，不是另写一份', () => {
        const route = larkChatResponseRoute();
        expect(larkChatResponseQueue()).toBe(route.queue);
    });
});

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

interface Amqp {
    channel: LarkResponseChannel & OutboundSubscriptionPort;
    declared: Route[];
    consuming: Map<string, (msg: ConsumeMessage) => Promise<void>>;
    /** 每一次 consume 都记一笔（consuming 是 Map，重复订阅会被它盖掉看不出来）。 */
    subscribeLog: string[];
    drained: string[];
    acked: string[];
    nacked: Array<{ id: string; requeue: boolean }>;
    /** 注入慢订阅：用来观察 reconcile 重入。 */
    beforeConsume?: () => Promise<void>;
}

function fakeAmqp(): Amqp {
    const declared: Route[] = [];
    const consuming = new Map<string, (msg: ConsumeMessage) => Promise<void>>();
    const subscribeLog: string[] = [];
    const drained: string[] = [];
    const acked: string[] = [];
    const nacked: Array<{ id: string; requeue: boolean }> = [];

    const amqp: Amqp = {
        declared,
        consuming,
        subscribeLog,
        drained,
        acked,
        nacked,
        channel: {
            declareRoute: async (route) => void declared.push(route),
            consume: async (queue, handler) => {
                await amqp.beforeConsume?.();
                subscribeLog.push(queue);
                consuming.set(queue, handler);
            },
            drainConsumer: async (queue) => {
                drained.push(queue);
                consuming.delete(queue);
            },
            ack: (msg) => void acked.push(idOf(msg)),
            nack: (msg, requeue) => void nacked.push({ id: idOf(msg), requeue }),
        },
    };
    return amqp;
}

function idOf(msg: ConsumeMessage): string {
    return String((msg as unknown as { id?: string }).id ?? '?');
}

function message(
    id: string,
    body: unknown,
    headers?: Record<string, unknown>,
): ConsumeMessage {
    return {
        id,
        content: Buffer.from(typeof body === 'string' ? body : JSON.stringify(body)),
        fields: { consumerTag: 'tag-1' },
        properties: { headers },
    } as unknown as ConsumeMessage;
}

function larkResponse(overrides: Partial<LarkChatResponse> = {}): LarkChatResponse {
    return {
        channel: 'lark',
        session_id: 'sess-1',
        message_id: 'cm_trigger',
        chat_id: 'cc_group',
        is_p2p: false,
        content: '在的',
        status: 'success',
        part_index: 0,
        is_last: true,
        bot_name: 'chiwei',
        ...overrides,
    };
}

interface Consumer {
    amqp: Amqp;
    delivered: Array<{ response: LarkChatResponse; lane?: string }>;
    queueDelays: number[];
    subscriptions: LarkOutboundSubscriptions;
    /** 当前订阅的队列，没订阅时是 null。 */
    subscribed: string | null;
    /** 改这个值 = 在 Dynamic Config 里改这个 key 的值。 */
    setSwitch(raw: string): void;
    /** 让下一次读开关直接抛。 */
    breakSwitch(error: Error | null): void;
    /** 再读一次开关并按结果增订 / 移交。 */
    reconcile(): Promise<void>;
    push(msg: ConsumeMessage): Promise<void>;
}

/** 本文件关心的可调项。前三个属于队列这一头，后两个属于订阅那一头。 */
interface ConsumerOptions {
    deliver?: LarkResponseConsumerDeps['deliver'];
    observeQueueDelay?: LarkResponseConsumerDeps['observeQueueDelay'];
    now?: LarkResponseConsumerDeps['now'];
    lane?: string;
    readConsumeSwitch?: () => Promise<string>;
    switchValue?: string;
}

/**
 * 起一个消费者并做**一次** reconcile。
 *
 * `switchValue` 缺省是 'true'，因为绝大多数用例关心的是"已经在消费之后怎么样"；
 * 开关本身的用例自己传值。
 */
async function startConsumer(options: ConsumerOptions = {}): Promise<Consumer> {
    const { switchValue = 'true' } = options;
    const amqp = fakeAmqp();
    const delivered: Array<{ response: LarkChatResponse; lane?: string }> = [];
    const queueDelays: number[] = [];
    let raw = switchValue;
    let broken: Error | null = null;

    const binding = larkChatResponseBinding({
        amqp: amqp.channel,
        deliver: options.deliver ?? (async (response, lane) => void delivered.push({ response, lane })),
        observeQueueDelay: options.observeQueueDelay ?? ((seconds) => void queueDelays.push(seconds)),
        now: options.now,
    });

    const subscriptions = new LarkOutboundSubscriptions({
        amqp: amqp.channel,
        lane: options.lane,
        queues: [binding],
        readConsumeSwitch:
            options.readConsumeSwitch ??
            (async () => {
                if (broken) throw broken;
                return raw;
            }),
    });
    await subscriptions.reconcile();

    const consumer: Consumer = {
        amqp,
        delivered,
        queueDelays,
        subscriptions,
        get subscribed(): string | null {
            return subscriptions.subscribedQueues()[0] ?? null;
        },
        setSwitch: (value) => {
            raw = value;
        },
        breakSwitch: (error) => {
            broken = error;
        },
        reconcile: () => subscriptions.reconcile(),
        push: async (msg) => {
            const handler = amqp.consuming.get(subscriptions.subscribedQueues()[0] ?? '');
            if (!handler) throw new Error('nothing is consuming');
            await handler(msg);
        },
    };
    return consumer;
}

// ---------------------------------------------------------------------------
// 开关
// ---------------------------------------------------------------------------

describe('消费开关 — 默认关，且读不到时不许自己变宽', () => {
    it('关着的时候一条队列都不订阅、一次声明都不发', async () => {
        const c = await startConsumer({ switchValue: 'false' });

        expect(c.subscribed).toBeNull();
        expect(c.amqp.declared).toEqual([]);
        expect([...c.amqp.consuming.keys()]).toEqual([]);
    });

    it('配置读不到（抛异常）时按关处理 —— 绝不回落成"打开"', async () => {
        // 变宽的后果是静默的：cutover 窗口里 channel-server 仍然订阅着同一条飞书
        // 队列，两个消费者守着它，RabbitMQ 轮询把回复随机劈成两半。不报错、不留痕。
        const c = await startConsumer({
            readConsumeSwitch: async () => {
                throw new Error('paas-engine unreachable');
            },
        });

        expect(c.subscribed).toBeNull();
        expect([...c.amqp.consuming.keys()]).toEqual([]);
    });

    it('配置压根没建（空串）时按关处理 —— 这跟 fetch 失败在 SDK 里是同一个信号', async () => {
        // DynamicConfig.get 把 fetch 失败吞成默认值（空串），所以"读失败"和"没配这个
        // key"在这一层长得一模一样。两者的处置也必须一样：没拿到有效指令就不动。
        const c = await startConsumer({ switchValue: '' });

        expect(c.subscribed).toBeNull();
    });

    it('填了看不懂的值：同样按"没有有效指令"处理', async () => {
        const c = await startConsumer({ switchValue: '待定' });

        expect(c.subscribed).toBeNull();
    });

    it('打开时先声明队列再订阅 —— 订阅一条没声明的队列等于守着空气', async () => {
        const c = await startConsumer({ switchValue: 'true' });

        expect(c.subscribed).toBe(larkChatResponseQueue());
        expect(c.amqp.declared).toEqual([larkChatResponseRoute()]);
        expect([...c.amqp.consuming.keys()]).toEqual([larkChatResponseQueue()]);
    });

    it('泳道部署订阅的是带泳道后缀的那条', async () => {
        const laneCase = LARK_RESPONSE_CASES.find((c) => c.lane !== null)!;
        const c = await startConsumer({ lane: laneCase.lane! });

        expect(c.subscribed).toBe(laneCase.expect.queue);
    });
});

// ---------------------------------------------------------------------------
// 开关在运行期翻动：不重启进程
// ---------------------------------------------------------------------------

describe('开关翻动 — 原地增订 / 原地移交，不重启进程', () => {
    it('从关翻到开：同一个进程当场开始消费，队列里的消息立刻被处理', async () => {
        // 决策九的交接是「旧消费者 drain → 新消费者接手」，中间既不能重叠也不能有
        // 空窗。启动时读一次开关的话两条路都走不通：先起新的会和还没 drain 的
        // channel-server 抢同一条队列；先 drain 再起新的，则新进程连 PG / bot 目录 /
        // Redis / MQ 的这段时延就是空窗 —— 泳道队列带 10s TTL，堆过去就被 DLX 弹回
        // prod，泳道的回复从 prod 实例发出去。
        const c = await startConsumer({ switchValue: 'false' });
        expect(c.subscribed).toBeNull();

        c.setSwitch('true');
        await c.reconcile();

        expect(c.subscribed).toBe(larkChatResponseQueue());
        expect(c.amqp.declared).toEqual([larkChatResponseRoute()]);

        await c.push(message('m-flip', larkResponse()));
        expect(c.delivered).toHaveLength(1);
        expect(c.amqp.acked).toEqual(['m-flip']);
    });

    it('从开翻到关：走 drain 屏障把队列交回去，之后不再消费', async () => {
        // 回滚方向也不该要重启：basic.cancel → 等在途归零 → 才算真的交出去。
        const c = await startConsumer({ switchValue: 'true' });

        c.setSwitch('false');
        await c.reconcile();

        expect(c.amqp.drained).toEqual([larkChatResponseQueue()]);
        expect(c.subscribed).toBeNull();
    });

    it('一直开着：重复 reconcile 不会再订阅一次', async () => {
        // 同一个进程订两次等于自己跟自己分摊消息，prefetch 也翻倍。
        const c = await startConsumer({ switchValue: 'true' });

        await c.reconcile();
        await c.reconcile();

        expect(c.amqp.subscribeLog).toEqual([larkChatResponseQueue()]);
        expect(c.amqp.declared).toHaveLength(1);
    });

    it('已经在消费之后配置读不到：保持消费，不自己把队列扔了', async () => {
        // 这个方向的"变宽"是停止消费：一次 paas-engine 抖动就让飞书的回复没人消费，
        // 泳道队列 10 秒后弹回 prod。没有有效指令时保持上次结论。
        const c = await startConsumer({ switchValue: 'true' });

        c.breakSwitch(new Error('paas-engine unreachable'));
        await c.reconcile();

        expect(c.subscribed).toBe(larkChatResponseQueue());
        expect(c.amqp.drained).toEqual([]);
    });

    it('两次 reconcile 撞在一起：只订阅一次', async () => {
        // 定时器的间隔比 drain 的最坏耗时短，两次 reconcile 重叠是常态而非意外。
        const c = await startConsumer({ switchValue: 'false' });

        let release!: () => void;
        const slowSubscribe = new Promise<void>((resolve) => {
            release = resolve;
        });
        c.amqp.beforeConsume = () => slowSubscribe;
        c.setSwitch('true');

        const first = c.reconcile();
        const second = c.reconcile();
        release();
        await Promise.all([first, second]);

        expect(c.amqp.subscribeLog).toEqual([larkChatResponseQueue()]);
    });
});

// ---------------------------------------------------------------------------
// fail-closed
// ---------------------------------------------------------------------------

/**
 * 把队列接到**真的** deliver 上，但底下所有端口都只记账。
 *
 * 「拒绝之前一行库都没查、一个 API 都没调」只有这样才能真的被断言 —— 用一个假的
 * deliver 桩只能证明"我没调那个桩"。
 */
function tracedDelivery(): {
    deliver: (response: LarkChatResponse, lane?: string) => Promise<void>;
    touched: string[];
} {
    const touched: string[] = [];
    const note = (what: string) => {
        touched.push(what);
    };

    const deps: LarkDeliveryDeps = {
        store: {
            chatIdOf: async (id) => {
                note(`db:chatIdOf:${id}`);
                return 'oc_group';
            },
            omIdOf: async (id) => {
                note(`db:omIdOf:${id}`);
                return 'om_trigger';
            },
            commonMessageIdOf: async (id) => {
                note(`db:commonMessageIdOf:${id}`);
                return null;
            },
            insertCommonMessage: async () => note('db:insertCommonMessage'),
            insertLarkMessage: async () => note('db:insertLarkMessage'),
            atomically: async (run) => {
                note('db:atomically');
                return run({
                    chatIdOf: async () => 'oc_group',
                    omIdOf: async () => 'om_trigger',
                    commonMessageIdOf: async () => null,
                    insertCommonMessage: async () => note('db:insertCommonMessage'),
                    insertLarkMessage: async () => note('db:insertLarkMessage'),
                });
            },
        },
        ledger: {
            find: async (sessionId) => {
                note(`db:ledger.find:${sessionId}`);
                return { session_id: sessionId, replies: [], safety_status: 'pending' };
            },
            appendReply: async () => note('db:ledger.appendReply'),
            settle: async () => note('db:ledger.settle'),
            settleSafety: async () => note('db:ledger.settleSafety'),
        },
        api: {
            sendPost: async () => {
                note('api:sendPost');
                return { messageId: 'om_sent' };
            },
            replyPost: async () => {
                note('api:replyPost');
                return { messageId: 'om_sent' };
            },
            recall: async () => note('api:recall'),
            uploadImage: async () => {
                note('api:uploadImage');
                return null;
            },
        },
        render: async (markdown) => {
            note('render');
            return [[{ tag: 'text', text: markdown }]] as unknown as PostContent;
        },
        botCommonUserId: (bot) => `cu_${bot}`,
        botDisplayName: () => undefined,
        newCommonId: () => 'cm_new',
        now: () => 1_700_000_000_000,
        wait: async () => {},
        speakAs: async (_who, say) => say(),
        observe: () => {},
    };

    return {
        touched,
        deliver: (response, lane) => deliverLarkChatResponse(deps, response, lane),
    };
}

describe('fail-closed — 不是飞书的消息一律拒绝', () => {
    it('别的渠道的 payload：拒绝、不 requeue，且一行库不查、一个 API 不调', async () => {
        const traced = tracedDelivery();
        const c = await startConsumer({ deliver: traced.deliver });

        await c.push(message('m1', larkResponse({ channel: 'qq' })));

        // requeue 会让两个服务把同一条消息推来推去，压成活锁；prod 队列挂着 DLX，
        // 丢过去还能查、能重放。
        expect(c.amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        expect(c.amqp.acked).toEqual([]);
        expect(traced.touched).toEqual([]);
    });

    it('压根没写 channel 的 payload：同样拒绝', async () => {
        // 这条分区队列是新建的，唯一的生产者（agent-service 的 sink_dispatch）在
        // payload 没有 channel 时**拒绝发布**，所以这里根本不存在"老信封"。
        // 兜底成 lark 等于把分流错误重新变成静默错投 —— 分区本来就是为了杀掉它。
        const traced = tracedDelivery();
        const c = await startConsumer({ deliver: traced.deliver });

        await c.push(message('m2', larkResponse({ channel: undefined })));

        expect(c.amqp.nacked).toEqual([{ id: 'm2', requeue: false }]);
        expect(traced.touched).toEqual([]);
    });

    it('channel 是空串：同样拒绝', async () => {
        const traced = tracedDelivery();
        const c = await startConsumer({ deliver: traced.deliver });

        await c.push(message('m3', larkResponse({ channel: '' })));

        expect(c.amqp.nacked).toEqual([{ id: 'm3', requeue: false }]);
        expect(traced.touched).toEqual([]);
    });

    it('自己的 payload 照常走完整条链', async () => {
        const traced = tracedDelivery();
        const c = await startConsumer({ deliver: traced.deliver });

        await c.push(message('m4', larkResponse()));

        expect(c.amqp.acked).toEqual(['m4']);
        expect(c.amqp.nacked).toEqual([]);
        expect(traced.touched).toContain('api:replyPost');
    });
});

// ---------------------------------------------------------------------------
// ACK 策略
// ---------------------------------------------------------------------------

describe('ACK 策略', () => {
    it('JSON 解析不了：拒绝、不 requeue（进 DLQ）', async () => {
        const c = await startConsumer();

        await c.push(message('m5', '{ 这不是 json'));

        expect(c.amqp.nacked).toEqual([{ id: 'm5', requeue: false }]);
        expect(c.delivered).toEqual([]);
    });

    it('处理成功：ACK 一次', async () => {
        const c = await startConsumer();

        await c.push(message('m6', larkResponse()));

        expect(c.amqp.acked).toEqual(['m6']);
        expect(c.amqp.nacked).toEqual([]);
    });

    it('deliver 自己吞掉的失败（发送 / 落库）仍然 ACK', async () => {
        // 出站失败不重投是刻意的：重投会让"已经发出去一半的分段消息"再发一遍。
        const c = await startConsumer({ deliver: async () => {} });

        await c.push(message('m7', larkResponse()));

        expect(c.amqp.acked).toEqual(['m7']);
    });

    it('deliver 往外抛（台账那次读挂了）：拒绝、不 requeue', async () => {
        const c = await startConsumer({
            deliver: async () => {
                throw new Error('pg is down');
            },
        });

        await c.push(message('m8', larkResponse()));

        expect(c.amqp.nacked).toEqual([{ id: 'm8', requeue: false }]);
        expect(c.amqp.acked).toEqual([]);
    });
});

// ---------------------------------------------------------------------------
// 随身上下文
// ---------------------------------------------------------------------------

describe('泳道与队列积压', () => {
    it('泳道只认 AMQP header', async () => {
        const c = await startConsumer();

        await c.push(
            message('m9', larkResponse({ lane: 'ppe-stale' }), { lane: 'ppe-real' }),
        );

        // body 里的 lane 是 agent-service 写给别的用途的字段；泳道队列带 TTL + DLX，
        // header 是唯一能穿过降级还保持原样的载体。
        expect(c.delivered[0]!.lane).toBe('ppe-real');
    });

    it('header 没写 lane：按 prod 走，不回落 body', async () => {
        const c = await startConsumer();

        await c.push(message('m10', larkResponse({ lane: 'ppe-stale' })));

        expect(c.delivered[0]!.lane).toBeUndefined();
    });

    it('published_at 换算成队列积压秒数', async () => {
        const c = await startConsumer({ now: () => 1_700_000_002_500 });

        await c.push(message('m11', larkResponse({ published_at: 1_700_000_000_000 })));

        expect(c.queueDelays).toEqual([2.5]);
    });

    it('没有 published_at 就不记 —— 记一个编出来的 0 会把积压曲线压平', async () => {
        const c = await startConsumer();

        await c.push(message('m12', larkResponse()));

        expect(c.queueDelays).toEqual([]);
    });
});
