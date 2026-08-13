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
// ## 两条队列，因为所有权正在从 lane 迁到 channel + lane
//
// 分区前的 `inbound_lane.{lane}` 只按 lane 分，但 owner 实际按 **channel + lane** 分：
// QQ 的信封和飞书的信封躺在同一条队列里，channel-server 和本服务竞争消费。抢到对方的
// 信封时 ACK 就等于把它吃掉 —— 对面永远收不到，而且没有任何报错。
//
// 根治办法是队列也按 channel + lane 分：`inbound_lane.{channel}.{lane}`，不同 owner
// 根本不共享队列，竞争消费自然不存在。换队列名不可能原子发布（生产者和消费者在不同的
// Deployment 里），所以走"消费侧先双订阅 → 切生产者 → 旧队列排空 → 移交"，本文件承担
// 头一步：默认只订阅共享队列，开关打开后同时订阅分区队列。
//
// 于是同一段 channel 校验在两条队列上有两种结论：
//
//   共享队列    退回去（`nack(requeue)`），对面还在订阅这条队列，交接得掉
//   分区队列    丢掉并吼（`nack(requeue=false)`），**这是一条断言**：分区之后不该
//               再有别人的信封进来。这条队列没有第二个消费者，退回去只会永远弹，而
//               prefetch=1 会让它把整条泳道堵死
//
// 共享队列上退回的代价要说清楚：RabbitMQ 竞争消费下 requeue 可能被随机分回自己。对面
// 在线时一两次就交接完；对面不在线时会一直弹回来 —— 所以**只在消息是重投时才等一下**，
// 把热循环压成慢轮询。消息始终留在队列里，不丢。
//
// 两条队列的声明都是 fail-closed 的：**不配 TTL、不配死信**。装在里面的是"已经判定该
// 在这条泳道处理"的消息，过期跑回 prod 就是拿泳道的改动去污染线上。

import { DynamicConfig } from '@inner/shared';
import { getRabbitChannel } from '@inner/shared/mq';

import { laneClaimStore, type InboundLaneStore } from './lane-claim';
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

/** 别人的信封弹回来之后等多久再退回去。压热循环用的，不是重试退避。 */
const FOREIGN_RETRY_DELAY_MS = 1_000;

/** 老信封没有 channel 字段，那个年代只有飞书在用这个队列。 */
const LEGACY_ENVELOPE_CHANNEL = 'lark';

/** 信封的归属渠道。老信封没有这个字段，那个年代只有飞书在用这个队列。 */
export function envelopeChannel(envelope: InboundLaneEnvelope): string {
    return envelope.channel ?? LEGACY_ENVELOPE_CHANNEL;
}

/**
 * 分区后的队列名。owner 是 channel + lane，队列也就按 channel + lane 分。
 *
 * ⚠️ 这个名字是**跨服务契约**：channel-server 那侧要拼出逐字相同的名字，否则两个服务
 * 各守着一条对方不认识的队列。两个 app 是两个包，编译期对不上，只能两边各钉一条断言。
 */
export function inboundLaneQueueName(channel: string, lane: string): string {
    return `${QUEUE_PREFIX}.${channel}.${lane}`;
}

/** 分区前的队列名。切换窗口内还得订阅它，把旧队列里的存量收干净。 */
export function sharedInboundLaneQueueName(lane: string): string {
    return `${QUEUE_PREFIX}.${lane}`;
}

/**
 * 渠道 + 事件类型 + 全局消息 id + 泳道，唯一确定"这条事件在这条泳道上处理了一次"。
 *
 * **不含队列名**，这是刻意的：双订阅期间同一条消息可能从旧队列来、也可能从新队列来，
 * key 认队列的话两边各算一次，用户就会看到两条回复。
 *
 * channel 必须在 key 里：分区之前队列是共享的，两个渠道的同名事件不带 channel 会互相
 * 顶掉对方的完成标记；分区之后它是两个服务的 key 不重叠的依据。channel-server 用的是
 * 逐字相同的格式，换手重投才认得出自己处理过。
 */
export function inboundLaneDedupeKey(envelope: InboundLaneEnvelope): string {
    return [
        QUEUE_PREFIX,
        envelopeChannel(envelope),
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

const realWait = (ms: number): Promise<void> => Bun.sleep(ms);

/**
 * "是否订阅按 channel 分区的新队列"。默认关 —— 镜像可以先上线、先部署，什么时候真
 * 订阅是切换动作的一部分。
 *
 * 走 Dynamic Config 而不是 env：Release env 会被部署的 POST 清空，长期开关放在那里
 * 会在某次部署之后悄悄失效。
 *
 * 只在启动时读一次，因为订阅本身是启动动作：开关翻过来之后要重启消费者才生效。这一
 * 步是纯增量（多订一条队列，行为不变），重启无害。
 *
 * 启动时没有请求上下文，所以 Dynamic Config 按 **prod** 解析——它是一个全局的切换
 * 开关，不是按泳道分别打开的旋钮。给某条泳道单独配这个 key 不会生效。
 */
export const INBOUND_LANE_CHANNEL_CONSUME_FLAG = 'enable_inbound_lane_channel_consume';

const dynamicConfig = new DynamicConfig();

export function inboundLaneChannelConsumeEnabled(): Promise<boolean> {
    return dynamicConfig.getBool(INBOUND_LANE_CHANNEL_CONSUME_FLAG, false);
}

/** 一条订阅。`partitioned` 决定别人的信封是退回去还是丢掉（见文件头）。 */
interface LaneSubscription {
    queue: string;
    partitioned: boolean;
}

export async function startInboundLaneConsumer(
    scope: LaneConsumerScope,
    deliver: (event: LarkEvent) => Promise<void>,
    deps: {
        amqp?: LaneChannel;
        store?: InboundLaneStore;
        wait?: (ms: number) => Promise<void>;
        channelQueueEnabled?: () => Promise<boolean>;
    } = {},
): Promise<void> {
    const amqp = deps.amqp ?? (getRabbitChannel() as unknown as LaneChannel);
    const store = deps.store ?? laneClaimStore;
    const wait = deps.wait ?? realWait;
    const channelQueueEnabled = deps.channelQueueEnabled ?? inboundLaneChannelConsumeEnabled;

    const subscriptions: LaneSubscription[] = [
        { queue: sharedInboundLaneQueueName(scope.lane), partitioned: false },
    ];
    if (await channelQueueEnabled()) {
        subscriptions.push({
            queue: inboundLaneQueueName(scope.channel, scope.lane),
            partitioned: true,
        });
    }

    // prefetch 是这条 amqp channel 的属性，不是队列的：声明一次，两个消费者共用。
    await amqp.prefetch(1);

    for (const subscription of subscriptions) {
        await subscribe(subscription);
    }

    async function subscribe({ queue, partitioned }: LaneSubscription): Promise<void> {
        await amqp.assertQueue(queue, { durable: true });
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
                const channel = envelopeChannel(envelope);
                if (channel !== scope.channel) {
                    if (partitioned) {
                        // 断言：分区队列里只会有自己的信封。破了说明投递侧发错了队列，
                        // 而这条队列没有第二个消费者 —— 退回去只会永远弹。
                        throw new Unprocessable(
                            `the partitioned queue ${queue} must only ever hold ` +
                                `"${scope.channel}" envelopes, but this one says "${channel}" ` +
                                `(message=${envelope.global_message_id})`,
                        );
                    }
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
}
