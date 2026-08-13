// 撤回这一头：订哪条队列、这条消息归不归我、ACK 还是退回、重投怎么发出去。
//
// 队列名不在这里写字面量，而是接到 contracts/mq-channel-routes.json 那份**跨语言契约
// 向量**上 —— 生产者是 agent-service（Python），消费者是本服务（TS）。
//
// 撤回逻辑本身（撤哪几条、算不算成功、台账写什么）在 recall.test.ts。

import { describe, expect, it } from 'bun:test';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import type { ConsumeMessage } from 'amqplib';
import { context } from '@inner/shared/middleware';
import type { Route } from '@inner/shared/mq';

import type { LarkRecallOutcome, LarkRecallPayload, LarkRecallRequest } from './recall';
import { recallLarkResponse, type LarkRecallDeps } from './recall';
import {
    larkRecallBinding,
    larkRecallQueue,
    larkRecallRoute,
    RECALL_RETRY_HEADER,
    type LarkRecallChannel,
} from './recall-queue';

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

const CONTRACT_PATH = resolve(import.meta.dir, '../../../../../contracts/mq-channel-routes.json');
const contract = JSON.parse(readFileSync(CONTRACT_PATH, 'utf8')) as ChannelRouteContract;

const LARK_RECALL_CASES = contract.cases.filter(
    (c) => c.base === 'recall' && c.channel === 'lark',
);

describe('订阅哪条队列 — 接在跨语言契约向量上', () => {
    it('契约里确实有飞书的 recall（向量缩水要立刻暴露）', () => {
        expect(LARK_RECALL_CASES.length).toBeGreaterThan(0);
        expect(LARK_RECALL_CASES.some((c) => c.lane === null)).toBe(true);
        expect(LARK_RECALL_CASES.some((c) => c.lane !== null)).toBe(true);
    });

    for (const c of LARK_RECALL_CASES) {
        it(`${c.name}：本服务算出来的队列名与 rk 逐值命中`, () => {
            expect(larkRecallQueue(c.lane ?? undefined)).toBe(c.expect.queue);
            expect(larkRecallRoute().rk).toBe(contract.base_routes.recall!.rk + '.lark');
        });
    }

    it('订阅项用的 Route 就是队列名的来源，不是另写一份', () => {
        expect(larkRecallBinding(nullDeps()).route).toEqual(larkRecallRoute());
        expect(larkRecallQueue()).toBe(larkRecallRoute().queue);
    });
});

// ---------------------------------------------------------------------------
// 测试替身
// ---------------------------------------------------------------------------

interface Published {
    route: Route;
    body: Record<string, unknown>;
    delayMs?: number;
    headers?: Record<string, unknown>;
    lane?: string;
    /** 发这一条的时候上下文里的 trace。publish 真身取的就是它。 */
    traceInContext: string;
}

interface Amqp {
    channel: LarkRecallChannel;
    acked: string[];
    nacked: Array<{ id: string; requeue: boolean }>;
    published: Published[];
}

function fakeAmqp(): Amqp {
    const acked: string[] = [];
    const nacked: Array<{ id: string; requeue: boolean }> = [];
    const published: Published[] = [];

    return {
        acked,
        nacked,
        published,
        channel: {
            ack: (msg) => void acked.push(idOf(msg)),
            nack: (msg, requeue) => void nacked.push({ id: idOf(msg), requeue }),
            publish: async (route, body, delayMs, headers, lane) => {
                published.push({
                    route,
                    body,
                    delayMs,
                    headers,
                    lane,
                    // 真身 rabbitmqClient.publish 把 trace_id 从 AsyncLocalStorage 取出来
                    // 写进 header，调用方给的会被它盖掉。所以"重投带不带得上 trace"
                    // 取决于这一刻上下文里有没有东西。
                    traceInContext: context.getTraceId(),
                });
            },
        },
    };
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

function payload(overrides: Partial<LarkRecallPayload> = {}): LarkRecallPayload {
    return {
        channel: 'lark',
        session_id: 'sess-1',
        reason: 'unsafe',
        detail: '违规内容',
        ...overrides,
    };
}

/** 只用来读 route / 类型的空壳。 */
function nullDeps(): Parameters<typeof larkRecallBinding>[0] {
    return {
        amqp: fakeAmqp().channel,
        recall: async () => ({ kind: 'short-circuited', status: 'recalled' }),
    };
}

interface Consumer {
    amqp: Amqp;
    /** 业务层收到的请求，按调用顺序。 */
    requests: LarkRecallRequest[];
    push(msg: ConsumeMessage): Promise<void>;
}

function startConsumer(
    recall: (request: LarkRecallRequest) => Promise<LarkRecallOutcome> = async () => ({
        kind: 'settled',
        status: 'recalled',
        recalled: 1,
        failed: 0,
    }),
): Consumer {
    const amqp = fakeAmqp();
    const requests: LarkRecallRequest[] = [];
    const binding = larkRecallBinding({
        amqp: amqp.channel,
        recall: (request) => {
            requests.push(request);
            return recall(request);
        },
    });
    const handler = binding.handler(larkRecallQueue());
    return { amqp, requests, push: (msg) => handler(msg) };
}

// ---------------------------------------------------------------------------
// fail-closed
// ---------------------------------------------------------------------------

/**
 * 把队列接到**真的** recallLarkResponse 上，但底下所有端口都只记账。
 *
 * 「拒绝之前一行库都没查、一个飞书 API 都没调」只有这样才能真的被断言 —— 用一个假的
 * recall 桩只能证明"我没调那个桩"。
 */
function tracedRecall(): {
    recall: (request: LarkRecallRequest) => Promise<LarkRecallOutcome>;
    touched: string[];
} {
    const touched: string[] = [];
    const note = (what: string): void => void touched.push(what);

    const deps: LarkRecallDeps = {
        ledger: {
            find: async (sessionId) => {
                note(`db:ledger.find:${sessionId}`);
                return {
                    session_id: sessionId,
                    bot_name: 'chiwei',
                    replies: [{ common_message_id: 'cm_a', sent_at: 'ts' }],
                    safety_status: 'pending',
                };
            },
            settleSafety: async () => note('db:ledger.settleSafety'),
        },
        store: {
            omIdOf: async (id) => {
                note(`db:omIdOf:${id}`);
                return 'om_a';
            },
        },
        api: { recall: async () => note('api:recall') },
        speakAs: async (_who, say) => say(),
        now: () => 1_700_000_000_000,
    };

    return { touched, recall: (request) => recallLarkResponse(deps, request) };
}

describe('fail-closed — 不是飞书的撤回一律拒绝', () => {
    // 共库方案下 common_agent_response 没有 channel 列，DB 层拦不住越界写入，隔离
    // 完全依赖「生产者的 rk 分对了」。撤回写的正是 safety 那两列 —— 越界处理写脏的是
    // 另一个服务的台账。
    for (const [name, channel] of [
        ['别的渠道', 'qq'],
        ['压根没写 channel', undefined],
        ['channel 是空串', ''],
    ] as const) {
        it(`${name}：拒绝、不 requeue，且一行库不查、一个 API 不调`, async () => {
            const traced = tracedRecall();
            const c = startConsumer(traced.recall);

            await c.push(message('m1', payload({ channel })));

            // requeue 会让两个服务把同一条消息推来推去，压成活锁；prod 队列挂着 DLX，
            // 丢过去还能查、能重放。
            expect(c.amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
            expect(c.amqp.acked).toEqual([]);
            expect(traced.touched).toEqual([]);
            expect(c.requests).toEqual([]);
        });
    }

    it('自己的 payload 照常走完整条链', async () => {
        const traced = tracedRecall();
        const c = startConsumer(traced.recall);

        await c.push(message('m2', payload()));

        expect(c.amqp.acked).toEqual(['m2']);
        expect(traced.touched).toContain('api:recall');
    });

    it('JSON 解析不了：拒绝、不 requeue（进 DLQ）', async () => {
        const c = startConsumer();

        await c.push(message('m3', '{ 这不是 json'));

        expect(c.amqp.nacked).toEqual([{ id: 'm3', requeue: false }]);
        expect(c.requests).toEqual([]);
    });
});

// ---------------------------------------------------------------------------
// 处置：ACK / 退回 / 重投
// ---------------------------------------------------------------------------

describe('处置 — 业务层的结论决定 ACK 还是退回', () => {
    it('撤过了：ACK', async () => {
        const c = startConsumer(async () => ({
            kind: 'settled',
            status: 'recalled',
            recalled: 2,
            failed: 0,
        }));

        await c.push(message('m1', payload()));

        expect(c.amqp.acked).toEqual(['m1']);
        expect(c.amqp.nacked).toEqual([]);
        expect(c.amqp.published).toEqual([]);
    });

    it('之前就已经是终态：ACK，什么都不重投', async () => {
        const c = startConsumer(async () => ({ kind: 'short-circuited', status: 'recalled' }));

        await c.push(message('m2', payload()));

        expect(c.amqp.acked).toEqual(['m2']);
        expect(c.amqp.published).toEqual([]);
    });

    it('重投到顶：退回、不 requeue（进死信），且不再重投一条', async () => {
        const c = startConsumer(async () => ({ kind: 'exhausted' }));

        await c.push(message('m3', payload()));

        expect(c.amqp.nacked).toEqual([{ id: 'm3', requeue: false }]);
        expect(c.amqp.acked).toEqual([]);
        expect(c.amqp.published).toEqual([]);
    });

    it('业务层往外抛：不 ACK 也不自己 nack，交给 MQ 客户端统一处置', async () => {
        // 客户端的 consume 包了一层 try/catch，抛出去等于 nack(requeue=false)。这里
        // 自己再 nack 一次就是对同一条消息 nack 两遍。
        const c = startConsumer(async () => {
            throw new Error('pg is down');
        });

        await expect(c.push(message('m4', payload()))).rejects.toThrow('pg is down');
        expect(c.amqp.acked).toEqual([]);
        expect(c.amqp.nacked).toEqual([]);
    });
});

describe('延时重投', () => {
    const retrying: LarkRecallOutcome = { kind: 'retry', delayMs: 5000, retryCount: 1 };

    it('投回**同 channel** 的队列，带上延时，然后 ACK 原消息', async () => {
        // 投回不带 channel 维度的老队列等于把消息倒退回共享队列，那边的消费者是
        // channel-server。
        const c = startConsumer(async () => retrying);

        await c.push(message('m1', payload()));

        expect(c.amqp.published).toHaveLength(1);
        expect(c.amqp.published[0]!.route).toEqual(larkRecallRoute());
        expect(c.amqp.published[0]!.delayMs).toBe(5000);
        expect(c.amqp.published[0]!.body).toEqual(payload() as unknown as Record<string, unknown>);
        expect(c.amqp.acked).toEqual(['m1']);
    });

    it('随行 header 只写重试计数 —— lane / trace 由 publish 自己注入', async () => {
        // 两处都写 lane header 会让"谁负责注入"变模糊，两份口径迟早漂移。全量比对。
        const c = startConsumer(async () => retrying);

        await c.push(message('m2', payload(), { lane: 'ppe-x', trace_id: 'trace-in' }));

        expect(c.amqp.published[0]!.headers).toEqual({ [RECALL_RETRY_HEADER]: 1 });
    });

    it('重投目标泳道用入站 header 的 lane', async () => {
        const c = startConsumer(async () => retrying);

        await c.push(message('m3', payload({ lane: 'ppe-stale' }), { lane: 'ppe-real' }));

        expect(c.amqp.published[0]!.lane).toBe('ppe-real');
    });

    it('入站没有 lane：**显式**投回 prod，不留给 publish 回落进程 LANE', async () => {
        // publish 对 undefined 会回落 env LANE，而 prod 实例接手降级消息时那正是要命的
        // 误判 —— 泳道的撤回会被投进泳道队列，可原本它就该回 prod。
        const c = startConsumer(async () => retrying);

        await c.push(message('m4', payload({ lane: 'ppe-stale' })));

        expect(c.amqp.published[0]!.lane).toBe('prod');
    });

    it('计数从入站 header 接着往上加', async () => {
        const c = startConsumer(async (request) => ({
            kind: 'retry',
            delayMs: 10000,
            retryCount: request.retryCount + 1,
        }));

        await c.push(message('m5', payload(), { [RECALL_RETRY_HEADER]: 1 }));

        expect(c.requests[0]!.retryCount).toBe(1);
        expect(c.amqp.published[0]!.headers).toEqual({ [RECALL_RETRY_HEADER]: 2 });
    });

    it('header 上的计数不是数字：当没重投过', async () => {
        const c = startConsumer(async () => retrying);

        await c.push(message('m6', payload(), { [RECALL_RETRY_HEADER]: 'twice' }));

        expect(c.requests[0]!.retryCount).toBe(0);
    });
});

// ---------------------------------------------------------------------------
// 随身上下文
// ---------------------------------------------------------------------------

describe('泳道与 trace 从 AMQP header 恢复', () => {
    it('lane 只认 header，body 里的那个不采纳', async () => {
        const c = startConsumer();

        await c.push(message('m1', payload({ lane: 'ppe-stale' }), { lane: 'ppe-real' }));

        expect(c.requests[0]!.lane).toBe('ppe-real');
    });

    it('header lane 是空串或缺失：视为 prod', async () => {
        const c = startConsumer();

        await c.push(message('m2', payload({ lane: 'ppe-stale' }), { lane: '' }));
        await c.push(message('m3', payload({ lane: 'ppe-stale' })));

        expect(c.requests.map((r) => r.lane)).toEqual([undefined, undefined]);
    });

    it('trace 从 header 恢复，业务层据此把撤回挂在同一条链上', async () => {
        const c = startConsumer();

        await c.push(message('m4', payload(), { trace_id: 'trace-inbound' }));

        expect(c.requests[0]!.traceId).toBe('trace-inbound');
    });

    it('整条处理跑在入站 trace 的上下文里 —— 重投的 trace 取自这里', async () => {
        // publish 真身从 AsyncLocalStorage 取 trace_id 写进 header。重投分支跑在
        // context 之外的话，写进去的就是空串，真实重试路径上 trace 链断掉。
        const c = startConsumer(async () => ({ kind: 'retry', delayMs: 5000, retryCount: 1 }));

        await c.push(message('m5', payload(), { trace_id: 'trace-inbound' }));

        expect(c.amqp.published[0]!.traceInContext).toBe('trace-inbound');
    });

    it('入站没有 trace_id：铸一条新的，不写空串', async () => {
        const c = startConsumer(async () => ({ kind: 'retry', delayMs: 5000, retryCount: 1 }));

        await c.push(message('m6', payload()));

        expect(c.amqp.published[0]!.traceInContext).not.toBe('');
        // 业务层拿到的和上下文里的是同一条，否则内外两层是两条不同的 trace。
        expect(c.requests[0]!.traceId).toBe(c.amqp.published[0]!.traceInContext);
    });

    it('上下文不外泄：处理完之后什么都不剩', async () => {
        const c = startConsumer();

        await c.push(message('m7', payload(), { trace_id: 'trace-inbound', lane: 'ppe-x' }));

        expect(context.getTraceId()).toBe('');
        expect(context.getLane()).toBe('');
    });
});
