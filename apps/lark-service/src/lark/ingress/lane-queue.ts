// 入口三：泳道信封队列 `inbound_lane.{lane}`。
//
// 语义跟另外两个入口不一样，别按同一套读：
//
//   webhook / 长连  飞书刚发生的事，本进程第一次见到它
//   本入口          **另一个进程已经收下并判定"这条该由 X 泳道处理"的事件的重放**
//
// 由此有三点差别：
//   1. **不做审计落库** —— 原始报文在它第一次进来时就已经记过了。
//   2. **按 channel + 事件 + 消息 + lane 去重** —— MQ 是 at-least-once。
//   3. **失败要让 MQ 知道** —— 另外两个入口已经向飞书应答过了，只能记日志；这里还
//      能把消息退回去。
//
// ## 队列的所有权是错的，本文件只是即时防线
//
// 队列**按 lane 分区**，但拆分后 owner 实际**按 channel + lane 分区**：QQ 的信封和
// 飞书的信封躺在同一个 `inbound_lane.{lane}` 里，channel-server 和本服务竞争消费。
// 抢到对方的信封时 ACK 就等于把它吃掉 —— 对面永远收不到，而且没有任何报错。
//
// 根治办法是队列按 channel + lane 分区（不同 owner 根本不共享队列），那要同时改投递
// 侧，跟"出站按 channel 分 routing key"是同一件事，放在 Task C。**在那之前**本文件
// 校验信封的 channel：不属于自己的绝不 ACK，退回队列等它真正的主人。分区做完之后这
// 段校验会退化成一条断言（那时不该再有别人的信封进来），不要删掉。
//
// 退回的代价要说清楚：RabbitMQ 竞争消费下 requeue 可能被随机分回自己。对面在线时一
// 两次就交接完；对面不在线时会一直弹回来 —— 所以**只在消息是重投时才等一下**，把热
// 循环压成慢轮询。消息始终留在队列里，不丢。
//
// 队列声明是 fail-closed 的：**不配 TTL、不配死信**。装在里面的是"已经判定该在这条
// 泳道处理"的消息，过期跑回 prod 就是拿泳道的改动去污染线上。

import { getRedisClient } from '@inner/shared/cache';
import { getRabbitChannel } from '@inner/shared/mq';

import { UnprocessableLarkEvent, type LarkEvent } from './lark-event';

/**
 * 队列里的信封。这是**跨服务的线格式**：切换期间投递方还在 channel-server，
 * 消费方已经是本服务，两边靠这些字段名对上。
 */
export interface InboundLaneEnvelope {
    /** 目标渠道。老信封没有这个字段，那时只有飞书在用这个队列。 */
    channel?: string;
    event_type: string;
    /** 全局消息 id，只当幂等 key 用。 */
    global_message_id: string;
    /** 跨泳道走 MQ，没有 HTTP header 可以透传 trace，只能写进信封。 */
    trace_id: string;
    lane: string;
    /** 投递这条消息的 bot。缺了它，下游读不到 bot 身份。 */
    bot_name: string;
    /** 飞书原始事件体，原样透传。 */
    params: unknown;
}

/** 消费者用到的 amqplib 表面，就这五个方法。 */
export interface LaneChannel {
    assertQueue(queue: string, options: { durable: boolean }): Promise<unknown>;
    prefetch(count: number): Promise<unknown>;
    consume(
        queue: string,
        onMessage: (
            msg: { content: Buffer; fields?: { redelivered?: boolean } } | null,
        ) => Promise<void>,
    ): Promise<unknown>;
    ack(msg: unknown): void;
    nack(msg: unknown, allUpTo: boolean, requeue: boolean): void;
}

/**
 * 认领一条消息的结果。
 *   claimed    占到了，可以开始处理
 *   in-flight  别人正在处理（或者上一个持有者崩了、租约还没到期）
 *   done       已经处理完了
 */
export type LaneClaim = 'claimed' | 'in-flight' | 'done';

/**
 * 幂等占位。**必须是原子的**：先查再写是两步，两个 Pod 能同时穿过查询、各执行一遍
 * 副作用。
 */
export interface InboundLaneStore {
    claim(key: string): Promise<LaneClaim>;
    /** 处理成功，占位转成长期的完成标记。 */
    complete(key: string): Promise<void>;
    /** 处理失败，立刻释放占位，让重投能重来。 */
    release(key: string): Promise<void>;
}

/** 本消费者负责的范围。信封落在范围外的一律不碰。 */
export interface LaneConsumerScope {
    /** 本服务负责的渠道。 */
    channel: string;
    /** 本进程所在的泳道。 */
    lane: string;
    /** 本服务认领了哪些事件类型。 */
    handles(eventType: string): boolean;
}

const QUEUE_PREFIX = 'inbound_lane';

/**
 * 占位租约。短，因为它回答的是"持有者崩了之后多久能被别人重新处理"；长了会让一条
 * 消息在持有者已经死掉的情况下白等。
 */
const CLAIM_LEASE_SECONDS = 5 * 60;

/** 完成标记的存活时间要盖住 MQ 的重投窗口。 */
const COMPLETED_TTL_SECONDS = 24 * 60 * 60;

/** 别人的信封弹回来之后等多久再退回去。压热循环用的，不是重试退避。 */
const FOREIGN_RETRY_DELAY_MS = 1_000;

/** 老信封没有 channel 字段，那个年代只有飞书在用这个队列。 */
const LEGACY_ENVELOPE_CHANNEL = 'lark';

const CLAIM_IN_FLIGHT = 'in-flight';
const CLAIM_DONE = 'done';

export function inboundLaneQueueName(lane: string): string {
    return `${QUEUE_PREFIX}.${lane}`;
}

/**
 * 渠道 + 事件类型 + 全局消息 id + 泳道，唯一确定"这条事件在这条泳道上处理了一次"。
 *
 * channel 必须在 key 里：队列是共享的，两个渠道的同名事件不带 channel 会互相顶掉
 * 对方的完成标记。
 */
export function inboundLaneDedupeKey(envelope: InboundLaneEnvelope): string {
    return [
        QUEUE_PREFIX,
        envelope.channel ?? LEGACY_ENVELOPE_CHANNEL,
        envelope.event_type,
        envelope.global_message_id,
        envelope.lane,
    ].join(':');
}

/** 这条消息永远处理不了，退回去也没用。 */
class Unprocessable extends Error {}

const REQUIRED_FIELDS = ['event_type', 'global_message_id', 'lane', 'bot_name'] as const;

function readEnvelope(raw: string): InboundLaneEnvelope {
    let parsed: unknown;
    try {
        parsed = JSON.parse(raw);
    } catch {
        throw new Unprocessable('envelope is not JSON');
    }
    if (typeof parsed !== 'object' || parsed === null) {
        throw new Unprocessable('envelope is not an object');
    }
    const envelope = parsed as Record<string, unknown>;
    for (const field of REQUIRED_FIELDS) {
        if (typeof envelope[field] !== 'string' || (envelope[field] as string).length === 0) {
            throw new Unprocessable(`envelope is missing "${field}"`);
        }
    }
    // 没有 params 的信封以前会一路走到解析层、解析出 null，然后被当成"处理成功"
    // ACK 掉 —— 又一条静默丢失。
    if (typeof envelope.params !== 'object' || envelope.params === null) {
        throw new Unprocessable('envelope carries no event payload');
    }
    return parsed as InboundLaneEnvelope;
}

/** 范围校验，两条都属于"投递方错了，退回去也还是错的"。 */
function checkScope(envelope: InboundLaneEnvelope, scope: LaneConsumerScope): void {
    if (envelope.lane !== scope.lane) {
        throw new Unprocessable(
            `envelope is addressed to lane "${envelope.lane}" but arrived on "${scope.lane}"`,
        );
    }
    // 本服务不认领这个事件类型。以前是打一条"没人处理"的日志然后 ACK —— 静默丢失。
    if (!scope.handles(envelope.event_type)) {
        throw new Unprocessable(`no handler claims event type "${envelope.event_type}"`);
    }
}

function larkEventOf(envelope: InboundLaneEnvelope): LarkEvent {
    return {
        type: envelope.event_type,
        payload: envelope.params,
        botName: envelope.bot_name,
        traceId: envelope.trace_id,
        lane: envelope.lane,
    };
}

const redisStore: InboundLaneStore = {
    claim: async (key) => {
        // SET key value EX ttl NX —— 一次往返，原子。
        const won = await getRedisClient().setNx(key, CLAIM_IN_FLIGHT, CLAIM_LEASE_SECONDS);
        if (won !== null) return 'claimed';
        // 没占到：要分清"已经做完了"和"有人正在做"。做完了可以安心 ACK；正在做的绝不
        // 能 ACK —— 对方还没写完成标记，ACK 会把消息销毁。
        const held = await getRedisClient().get(key);
        return held === CLAIM_DONE ? 'done' : 'in-flight';
    },
    complete: async (key) => {
        const redis = getRedisClient();
        await redis.set(key, CLAIM_DONE);
        await redis.expire(key, COMPLETED_TTL_SECONDS);
    },
    release: async (key) => {
        await getRedisClient().del(key);
    },
};

const realWait = (ms: number): Promise<void> => Bun.sleep(ms);

export async function startInboundLaneConsumer(
    scope: LaneConsumerScope,
    deliver: (event: LarkEvent) => Promise<void>,
    deps: {
        amqp?: LaneChannel;
        store?: InboundLaneStore;
        wait?: (ms: number) => Promise<void>;
    } = {},
): Promise<void> {
    const amqp = deps.amqp ?? (getRabbitChannel() as unknown as LaneChannel);
    const store = deps.store ?? redisStore;
    const wait = deps.wait ?? realWait;
    const queue = inboundLaneQueueName(scope.lane);

    await amqp.assertQueue(queue, { durable: true });
    await amqp.prefetch(1);
    await amqp.consume(queue, async (msg) => {
        if (!msg) return;
        const redelivered = msg.fields?.redelivered === true;

        /** 退回队列。只有重投过的才等一下 —— 正常交接不该背延迟成本。 */
        const giveBack = async (): Promise<void> => {
            if (redelivered) await wait(FOREIGN_RETRY_DELAY_MS);
            amqp.nack(msg, false, true);
        };

        let key: string | undefined;
        try {
            const envelope = readEnvelope(msg.content.toString());

            // ---- 所有权判断必须先于任何认领 ----
            // 认领别人的消息 = 替对面写下"这条处理过了"，比 ACK 掉还糟。
            const channel = envelope.channel ?? LEGACY_ENVELOPE_CHANNEL;
            if (channel !== scope.channel) {
                console.warn(
                    `[lane-queue] ${queue} holds a "${channel}" envelope; this service owns ` +
                        `"${scope.channel}" — handing it back ` +
                        `(message=${envelope.global_message_id})`,
                );
                await giveBack();
                return;
            }

            checkScope(envelope, scope);

            key = inboundLaneDedupeKey(envelope);
            const claim = await store.claim(key);
            if (claim === 'done') {
                console.info(`[lane-queue] already handled, skipping: ${key}`);
                amqp.ack(msg);
                return;
            }
            if (claim === 'in-flight') {
                // 别人正拿着（或者上一个持有者崩了、租约还没到期）。ACK 会在对方写下
                // 完成标记之前把消息销毁，只能退回去等租约到期。
                console.info(`[lane-queue] someone else is handling it, backing off: ${key}`);
                await giveBack();
                return;
            }

            await deliver(larkEventOf(envelope));
            await store.complete(key);
            amqp.ack(msg);
        } catch (error) {
            // 认领过就要还回去，否则重投的那一条会白等一个租约周期。
            if (key) {
                await store.release(key).catch((releaseError) => {
                    console.error(`[lane-queue] failed to release ${key}:`, releaseError);
                });
            }

            const permanent =
                error instanceof Unprocessable || error instanceof UnprocessableLarkEvent;
            if (permanent) {
                // prefetch 是 1：把一条永远处理不了的消息塞回队头，整条泳道就永远堵在
                // 它上面。只能丢，但要吼出来。
                console.error(
                    `[lane-queue] dropping an unprocessable message from ${queue}: ` +
                        `${(error as Error).message}`,
                );
                amqp.nack(msg, false, false);
                return;
            }

            console.error(`[lane-queue] ${queue} failed, requeueing:`, error);
            amqp.nack(msg, false, true);
        }
    });

    console.info(`[lane-queue] consuming ${queue} for channel=${scope.channel}`);
}
