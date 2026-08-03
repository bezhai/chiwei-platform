import { describe, expect, it } from 'bun:test';

import { UnprocessableLarkEvent, type LarkEvent } from './lark-event';
import {
    inboundLaneDedupeKey,
    inboundLaneQueueName,
    startInboundLaneConsumer,
    type InboundLaneEnvelope,
    type LaneChannel,
    type LaneClaim,
    type LaneConsumerScope,
} from './lane-queue';

const SCOPE: LaneConsumerScope = {
    channel: 'lark',
    lane: 'ppe-x',
    handles: (type) => type === 'im.message.receive_v1',
};

function envelope(overrides: Partial<InboundLaneEnvelope> = {}): InboundLaneEnvelope {
    return {
        channel: 'lark',
        event_type: 'im.message.receive_v1',
        global_message_id: 'cm_1',
        trace_id: 'trace-1',
        lane: 'ppe-x',
        bot_name: 'chiwei',
        params: { message: { message_id: 'om_1' } },
        ...overrides,
    };
}

function fakeAmqp() {
    const asserted: Array<{ queue: string; options: unknown }> = [];
    const acked: string[] = [];
    const nacked: Array<{ id: string; requeue: boolean }> = [];
    let prefetched = 0;
    let onMessage:
        | ((msg: { content: Buffer; fields?: { redelivered?: boolean } } | null) => Promise<void>)
        | null = null;

    const amqp: LaneChannel = {
        assertQueue: async (queue, options) => void asserted.push({ queue, options }),
        prefetch: async (count) => {
            prefetched = count;
        },
        consume: async (_queue, handler) => {
            onMessage = handler;
            return {};
        },
        ack: (msg) => void acked.push((msg as { id: string }).id),
        nack: (msg, _allUpTo, requeue) =>
            void nacked.push({ id: (msg as { id: string }).id, requeue }),
    };

    const push = async (id: string, env: unknown, redelivered = false) => {
        await onMessage!({
            content: Buffer.from(JSON.stringify(env)),
            fields: { redelivered },
            id,
        } as never);
    };

    return {
        amqp,
        asserted,
        acked,
        nacked,
        push,
        get prefetched() {
            return prefetched;
        },
    };
}

function fakeStore(initial: Record<string, 'in-flight' | 'done'> = {}) {
    const keys = new Map<string, 'in-flight' | 'done'>(Object.entries(initial));
    const calls: string[] = [];
    return {
        keys,
        calls,
        claim: async (key: string): Promise<LaneClaim> => {
            calls.push(`claim:${key}`);
            const held = keys.get(key);
            if (held) return held;
            keys.set(key, 'in-flight');
            return 'claimed';
        },
        complete: async (key: string) => {
            calls.push(`complete:${key}`);
            keys.set(key, 'done');
        },
        release: async (key: string) => {
            calls.push(`release:${key}`);
            keys.delete(key);
        },
    };
}

async function start(
    deliver: (event: LarkEvent) => Promise<void>,
    options: { store?: ReturnType<typeof fakeStore>; scope?: LaneConsumerScope } = {},
) {
    const amqp = fakeAmqp();
    const store = options.store ?? fakeStore();
    const waits: number[] = [];
    await startInboundLaneConsumer(options.scope ?? SCOPE, deliver, {
        amqp: amqp.amqp,
        store,
        wait: async (ms) => void waits.push(ms),
    });
    return { amqp, store, waits };
}

describe('inboundLaneQueueName', () => {
    it('names one queue per lane', () => {
        expect(inboundLaneQueueName('ppe-x')).toBe('inbound_lane.ppe-x');
    });
});

describe('inboundLaneDedupeKey', () => {
    // 队列按 lane 分区，但 owner 其实按 channel + lane 分区，所以幂等 key 也必须带上
    // channel —— 否则飞书和 QQ 的同名事件会互相顶掉对方的完成标记。
    it('identifies one processing of one event, per channel and lane', () => {
        expect(inboundLaneDedupeKey(envelope())).toBe(
            'inbound_lane:lark:im.message.receive_v1:cm_1:ppe-x',
        );
    });

    it('tells the same message on two lanes apart', () => {
        expect(inboundLaneDedupeKey(envelope({ lane: 'ppe-a' }))).not.toBe(
            inboundLaneDedupeKey(envelope({ lane: 'ppe-b' })),
        );
    });

    it('tells the same message on two channels apart', () => {
        expect(inboundLaneDedupeKey(envelope({ channel: 'qq' }))).not.toBe(
            inboundLaneDedupeKey(envelope({ channel: 'lark' })),
        );
    });
});

describe('startInboundLaneConsumer', () => {
    // 这个队列**绝不能**配 TTL + 死信回 prod：装在里面的是"已经判定该在这条泳道处理"
    // 的消息，过期跑回 prod 就是拿泳道的改动去污染线上。
    it('declares a durable queue that never expires messages back to prod', async () => {
        const { amqp } = await start(async () => {});
        expect(amqp.asserted).toEqual([{ queue: 'inbound_lane.ppe-x', options: { durable: true } }]);
        expect(amqp.prefetched).toBe(1);
    });

    it('rebuilds the event from the envelope, lane and all', async () => {
        const delivered: LarkEvent[] = [];
        const { amqp } = await start(async (e) => void delivered.push(e));

        await amqp.push('m1', envelope());

        expect(delivered).toEqual([
            {
                type: 'im.message.receive_v1',
                payload: { message: { message_id: 'om_1' } },
                botName: 'chiwei',
                traceId: 'trace-1',
                lane: 'ppe-x',
            },
        ]);
        expect(amqp.acked).toEqual(['m1']);
    });

    describe('messages that belong to another channel', () => {
        // inbound_lane.{lane} 是**按 lane 分区的队列，而 owner 实际按 channel + lane
        // 分区** —— QQ 的信封和飞书的信封躺在同一个队列里，两个服务竞争消费。抢到别人
        // 的信封时 ACK 就等于把它吃掉，对面永远收不到。
        it('never acknowledges a message it does not own', async () => {
            const delivered: LarkEvent[] = [];
            const { amqp, store } = await start(async (e) => void delivered.push(e));

            await amqp.push('m1', envelope({ channel: 'qq' }));

            expect(delivered).toEqual([]);
            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
            // 更要紧的是别去认领它：认领了就等于替对面写下"这条处理过了"。
            expect(store.calls).toEqual([]);
        });

        // 第一次拿到别人的信封就立刻退回去，让对面的消费者接手 —— 对面在线时这一下就
        // 交接完了，不该有任何延迟成本。
        it('hands a foreign message straight back on first sight', async () => {
            const { amqp, waits } = await start(async () => {});
            await amqp.push('m1', envelope({ channel: 'qq' }), false);
            expect(waits).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
        });

        // 又回到自己手上，说明对面不在线（或者被随机分回来了）。这时必须放慢，否则就是
        // 一个纯烧 CPU 的活锁。放慢不放弃：消息一直留在队列里等它的主人上线。
        it('slows down when a foreign message keeps coming back', async () => {
            const { amqp, waits } = await start(async () => {});
            await amqp.push('m1', envelope({ channel: 'qq' }), true);
            expect(waits).toHaveLength(1);
            expect(waits[0]).toBeGreaterThan(0);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
        });

        // 老信封没有 channel 字段。那个年代只有飞书在用这个队列，按飞书算。
        it('treats an envelope with no channel as its own', async () => {
            const delivered: LarkEvent[] = [];
            const { amqp } = await start(async (e) => void delivered.push(e));
            const legacy = envelope();
            delete legacy.channel;

            await amqp.push('m1', legacy);

            expect(delivered).toHaveLength(1);
            expect(amqp.acked).toEqual(['m1']);
        });
    });

    describe('claiming', () => {
        // 先占位再处理。反过来（查一下没处理过 → 处理 → 标记）是三步，两个 Pod 能同时
        // 穿过第一步，各执行一遍副作用。
        it('claims the message before doing any work', async () => {
            const store = fakeStore();
            const order: string[] = [];
            const { amqp } = await start(async () => void order.push('deliver'), { store });

            await amqp.push('m1', envelope());

            expect(store.calls[0]).toMatch(/^claim:/);
            expect(order).toEqual(['deliver']);
            expect(store.calls[store.calls.length - 1]).toMatch(/^complete:/);
        });

        it('skips and acknowledges something already finished', async () => {
            const key = inboundLaneDedupeKey(envelope());
            const store = fakeStore({ [key]: 'done' });
            const delivered: LarkEvent[] = [];
            const { amqp } = await start(async (e) => void delivered.push(e), { store });

            await amqp.push('m1', envelope());

            expect(delivered).toEqual([]);
            expect(amqp.acked).toEqual(['m1']);
        });

        // 另一个消费者正拿着它（或者上一个持有者崩了、租约还没到期）。绝不能 ACK ——
        // 那会在对方还没写下完成标记时把消息销毁。退回队列，等租约到期后重来。
        it('requeues rather than acknowledges while someone else holds the claim', async () => {
            const key = inboundLaneDedupeKey(envelope());
            const store = fakeStore({ [key]: 'in-flight' });
            const delivered: LarkEvent[] = [];
            const { amqp, waits } = await start(async (e) => void delivered.push(e), { store });

            await amqp.push('m1', envelope(), true);

            expect(delivered).toEqual([]);
            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
            expect(waits).toHaveLength(1); // 别原地空转
        });

        // 处理失败要立刻把占位还回去，否则重投的那一条会看到"有人在处理"，白等一个
        // 租约周期。
        it('releases the claim when processing fails', async () => {
            const store = fakeStore();
            const key = inboundLaneDedupeKey(envelope());
            const { amqp } = await start(
                async () => {
                    throw new Error('database is down');
                },
                { store },
            );

            await amqp.push('m1', envelope());

            expect(store.calls).toEqual([`claim:${key}`, `release:${key}`]);
            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: true }]);
        });

        it('lets the retry claim it again after a failure', async () => {
            const store = fakeStore();
            let attempts = 0;
            const { amqp } = await start(
                async () => {
                    attempts += 1;
                    if (attempts === 1) throw new Error('transient');
                },
                { store },
            );

            await amqp.push('m1', envelope());
            await amqp.push('m1', envelope(), true);

            expect(attempts).toBe(2);
            expect(amqp.acked).toEqual(['m1']);
        });
    });

    describe('messages that can never succeed', () => {
        // prefetch 是 1：把一条永远处理不了的消息塞回队头，整条泳道就永远堵在它上面。
        // 所以"这次不行"和"永远不行"必须分开：前者重投，后者丢掉但吼出来。
        const permanent = async (env: unknown) => {
            const delivered: LarkEvent[] = [];
            const { amqp, store } = await start(async (e) => void delivered.push(e));
            await amqp.push('m1', env);
            return { delivered, amqp, store };
        };

        it('rejects an envelope that is not even an object', async () => {
            const { amqp } = await permanent('nonsense');
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        it.each(['event_type', 'global_message_id', 'lane', 'bot_name'] as const)(
            'rejects an envelope with no %s',
            async (field) => {
                const broken = envelope();
                delete (broken as unknown as Record<string, unknown>)[field];
                const { amqp } = await permanent(broken);
                expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
            },
        );

        // 没有 params 的信封以前会一路走到解析层、解析出 null、然后被当成"处理成功"
        // ACK 掉 —— 又一条静默丢失。
        it('rejects an envelope carrying no event payload', async () => {
            const { amqp, delivered } = await permanent(envelope({ params: undefined }));
            expect(delivered).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        // 信封说该去别的泳道，却躺在我们的队列里：投递方错了，重投一万次也还是错的。
        it('rejects an envelope addressed to another lane', async () => {
            const { amqp } = await permanent(envelope({ lane: 'ppe-other' }));
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        // 本服务不认领这个事件类型。以前是"没人处理"打条日志然后 ACK —— 静默丢失。
        it('rejects an event type this service does not claim', async () => {
            const { amqp, delivered } = await permanent(
                envelope({ event_type: 'im.chat.updated_v1' }),
            );
            expect(delivered).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
        });

        // 永久失败一律不认领：认领了会留下一个没人清理的 in-flight 键。
        it('does not claim anything it rejects up front', async () => {
            const { store } = await permanent(envelope({ lane: 'ppe-other' }));
            expect(store.calls).toEqual([]);
        });

        // 载荷本身没救（比如消息事件里没有 message_id）：处理方抛 UnprocessableLarkEvent，
        // 跟"库连不上"这种下次可能就好了的失败区分开。
        it('rejects a payload the handler declares unprocessable, and frees the claim', async () => {
            const store = fakeStore();
            const { amqp } = await start(
                async () => {
                    throw new UnprocessableLarkEvent('event carries no message id');
                },
                { store },
            );

            await amqp.push('m1', envelope());

            expect(amqp.acked).toEqual([]);
            expect(amqp.nacked).toEqual([{ id: 'm1', requeue: false }]);
            expect(store.calls[store.calls.length - 1]).toMatch(/^release:/);
        });
    });
});
