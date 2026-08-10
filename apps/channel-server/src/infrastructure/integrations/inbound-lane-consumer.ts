// 入站 lane 消费者（lane-routing-redesign §4.3/§4.4）。lane channel-server 起这个
// 消费者，消费自己 lane 的信封队列。当前重放完整入站链（含 MessageTransferer、识图
// 管线、common bot presence upsert 等前置副作用）；抽出真正后半段是已知待办，本文件
// 暂不改变行为。
//
// 队列声明走 fail-closed 的 assertInboundLaneQueue（无 10s TTL、无 DLX 回 prod，§4.6）。
//
// 幂等（§4.4 point 5）：channel + event_type + globalMessageId + lane 唯一确定"这条
// 事件在这条泳道处理了一次"。协议是原子占位（claim → 处理 → complete），值和状态机与
// lark-service 逐字相同，见 inbound-lane-claim.ts —— 双订阅窗口里同一条信封可能被任
// 一方拿到，协议不一样就会一边丢消息、一边重复处理。key 不含队列名，所以同一条信封走
// 新队列还是旧队列都只处理一次。
//
// ## 两条队列，因为所有权正在从 lane 迁到 channel + lane
//
// 分区前的 inbound_lane.{lane} 只按 lane 分，但拆分后 owner 是 channel + lane：飞书
// 的信封归 lark-service，QQ 的归本服务，两个服务却在竞争消费同一条队列。换队列名不
// 可能原子发布（生产者和消费者在不同的 Deployment 里），所以走「消费侧先双订阅 → 切
// 生产者 → 旧队列排空 → 移交」，本文件承担头一步。
//
// ## 分区不等于不再竞争，还得有人真的不去收
//
// 本服务在 cutover 窗口内仍然注册着 lark runtime（代码删不掉，删了就只能回滚镜像），
// 所以"订阅面跟着 runtime 走"意味着整个窗口里两个服务都守着 inbound_lane.lark.{lane}
// —— 分区等于没做。移交因此是一个运行期的显式动作：拥有集合走 dynamic config
// （inbound-lane-channels.ts），订阅面按启动快照，**认领**逐条现读。
//
// 于是一条信封要过三关：
//
//   分区队列的归属断言   分区之后这条队列里只该有对应 channel 的信封；破了说明投递侧
//                        发错了队列，而这条队列没有第二个消费者——退回去只会永远弹，
//                        prefetch=1 会让它把整条泳道堵死。所以丢，但要吼出来
//   拥有集合             不在集合里就退回去（两种队列上都一样），等它真正的 owner 拿。
//                        自己处理掉 = 流量被随机劈成两半，不报错、不留痕
//   原子占位             同一条消息只有一个消费者能占到位

import { getRabbitChannel } from '@inner/shared/mq';
import {
    assertInboundLaneQueue,
    envelopeChannel,
    inboundDedupeKey,
    inboundLaneQueueName,
    sharedInboundLaneQueueName,
    type InboundLaneEnvelope,
} from './inbound-lane';
import { loadInboundLaneChannels } from './inbound-lane-channels';
import { inboundLaneStore, type InboundLaneStore } from './inbound-lane-claim';
import { isInboundLaneChannelConsumeEnabled } from './inbound-lane-flag';
import { context } from '@middleware/context';

export interface ConsumeDeps {
    // 幂等占位。协议（值、租约、状态机）是跨服务契约，见 inbound-lane-claim.ts。
    store: InboundLaneStore;
    // 入站处理。
    process: (env: InboundLaneEnvelope) => Promise<void>;
}

/**
 * 一条信封的下场。调用方据此决定 ACK 还是退回队列——`in-flight` **绝不能** ACK：
 * 对方还没写完成标记，ACK 会把消息销毁。
 */
export type LaneOutcome = 'handled' | 'already-done' | 'in-flight';

// 纯逻辑：注入 store/process，确定性测幂等分叉。
export async function consumeInboundLaneEnvelope(
    env: InboundLaneEnvelope,
    deps: ConsumeDeps,
): Promise<LaneOutcome> {
    const key = inboundDedupeKey(env);
    const claim = await deps.store.claim(key);
    if (claim === 'done') {
        console.info(`[inbound-lane] duplicate envelope skipped (already processed): ${key}`);
        return 'already-done';
    }
    if (claim === 'in-flight') {
        console.info(`[inbound-lane] someone else is handling it, backing off: ${key}`);
        return 'in-flight';
    }

    try {
        await deps.process(env);
    } catch (error) {
        // 认领过就要还回去，否则重投的那一条会白等一个租约周期。
        await deps.store.release(key).catch((releaseError) => {
            console.error(`[inbound-lane] failed to release ${key}:`, releaseError);
        });
        throw error;
    }
    await deps.store.complete(key);
    return 'handled';
}

// 分区队列上收到别人的信封。单独一个类型，因为它的处置跟"处理失败"相反：重投一万次
// 也还是错的，只能丢。
export class ForeignEnvelope extends Error {}

// 分区之后的不变量：这条队列里只会有 queueChannel 的信封。它需要被持续保证，不是可以
// 默认的事实 —— 所以留一条断言，而不是删掉校验。
export function assertEnvelopeChannel(
    env: InboundLaneEnvelope,
    queueChannel: string,
    queue: string,
): void {
    const channel = envelopeChannel(env);
    if (channel === queueChannel) return;
    throw new ForeignEnvelope(
        `${queue} must only ever hold "${queueChannel}" envelopes, but this one says ` +
            `"${channel}" (event=${env.event_type} gmid=${env.global_message_id})`,
    );
}

export interface InboundLaneConsumerOptions {
    // 本进程**能**处理哪些 channel 的入站信封。由调用方从已注册的 channel runtime 算
    // 出来，本模块不 import 插件（那会把 ORM/SDK 拉进来）。
    handles: string[];
    // 本进程**当前拥有**哪些 channel。默认在 handles 之内按 dynamic config 收窄。
    // 与 handles 分开，是因为 cutover 窗口内两者必然不等：代码还在（能处理），流量
    // 已经移交出去了（不该处理）。见 inbound-lane-channels.ts。
    loadOwnedChannels?: () => Promise<string[]>;
    // 是否订阅分区队列。默认读 dynamic config，默认关。订阅是启动动作，只在这里读一次
    // ——翻开之后要重启消费者才生效。
    channelQueueEnabled?: () => Promise<boolean>;
    // 别人的信封弹回来之后等多久再退回去。注入只为测试。
    wait?: (ms: number) => Promise<void>;
    // 幂等占位。默认走 redis，注入只为测试。
    store?: InboundLaneStore;
}

// 一条订阅。channel 只在分区队列上有值，它是"这条队列里只该有谁"的断言依据。
interface LaneSubscription {
    queue: string;
    channel?: string;
}

// 别人的信封弹回来之后等多久再退回去。压热循环用的，不是重试退避：对面在线时一两次
// 就交接完，对面不在线时才会一直弹回来。
const FOREIGN_RETRY_DELAY_MS = 1_000;

// 生产装配：起 fail-closed 队列消费者，幂等 + 重放入站处理。
// handleMessage 由调用方注入，避免本模块直接 import 飞书 handlers 把 ORM/SDK 拉进来。
export async function startInboundLaneConsumer(
    lane: string,
    handleEnvelope: (env: InboundLaneEnvelope) => Promise<void>,
    options: InboundLaneConsumerOptions,
): Promise<void> {
    const ch = getRabbitChannel();
    const channelQueueEnabled = options.channelQueueEnabled ?? isInboundLaneChannelConsumeEnabled;
    const loadOwnedChannels =
        options.loadOwnedChannels ?? (() => loadInboundLaneChannels(options.handles));
    const wait = options.wait ?? ((ms: number) => Bun.sleep(ms));
    const store = options.store ?? inboundLaneStore;

    // 订阅面用启动时的快照：退订要 basic.cancel + 等在途归零（决策九的 drain 屏障），
    // 不是这里能做的事。收窄之后这条订阅会空转到下次重启——无害，因为**认领**是逐条
    // 现读的（见下），队列上真来了信封也不会被处理。
    const subscriptions: LaneSubscription[] = [{ queue: sharedInboundLaneQueueName(lane) }];
    if (await channelQueueEnabled()) {
        for (const channel of await loadOwnedChannels()) {
            subscriptions.push({ queue: inboundLaneQueueName(channel, lane), channel });
        }
    }

    // prefetch 是这条 amqp channel 的属性，不是队列的：声明一次，几个消费者共用。
    await ch.prefetch(1);

    for (const subscription of subscriptions) {
        await subscribe(subscription);
    }

    async function subscribe({ queue, channel }: LaneSubscription): Promise<void> {
        await assertInboundLaneQueue(ch, queue);
        await ch.consume(queue, async (msg) => {
            if (!msg) return;
            const redelivered = msg.fields?.redelivered === true;

            // 退回队列。只有重投过的才等一下——正常交接不该背延迟成本，而一直弹回来
            // 说明对面不在线，那就把热循环压成慢轮询。消息始终留在队列里，不丢。
            const giveBack = async (): Promise<void> => {
                if (redelivered) await wait(FOREIGN_RETRY_DELAY_MS);
                ch.nack(msg, false, true);
            };

            try {
                const env = JSON.parse(msg.content.toString()) as InboundLaneEnvelope;
                if (channel) assertEnvelopeChannel(env, channel, queue);

                // ---- 所有权判断必须先于任何认领 ----
                // 认领别人的消息 = 替对面写下"这条处理过了"，比 ACK 掉还糟。
                // 逐条现读（dynamic config 有 10s 缓存），所以移交不需要重启：把
                // channel 从拥有集合里摘掉的那一刻起，它的信封就一律退回去等对面拿。
                const envChannel = envelopeChannel(env);
                const owned = await loadOwnedChannels();
                if (!owned.includes(envChannel)) {
                    console.warn(
                        `[inbound-lane] ${queue} holds a "${envChannel}" envelope; this service ` +
                            `owns [${owned.join(', ')}] — handing it back ` +
                            `(gmid=${env.global_message_id})`,
                    );
                    await giveBack();
                    return;
                }

                const outcome = await consumeInboundLaneEnvelope(env, {
                    store,
                    process: async (e) => {
                        // 从信封读出 bot_name + lane 注入 context（§6：跨 lane 用信封不用
                        // header）。botName 必须注入，否则入站处理 context.getBotName()
                        // 拿不到 bot 身份。然后走与现状一致的完整入站链；本进程
                        // lane==信封 lane，决策点会判 local，不会再次 dispatch（无自投循环）。
                        await context.run(
                            context.createContext(e.bot_name, e.trace_id, e.lane),
                            async () => {
                                await handleEnvelope(e);
                            },
                        );
                    },
                });
                if (outcome === 'in-flight') {
                    // 别人正拿着它。ACK 会在对方写下完成标记之前把消息销毁——那是真丢，
                    // 不是重复。退回去等租约到期。
                    await giveBack();
                    return;
                }
                ch.ack(msg);
            } catch (err) {
                if (err instanceof ForeignEnvelope) {
                    // 不变量被破坏。重投也还是错的，而且 prefetch=1 会让它堵死整条泳道。
                    console.error(`[inbound-lane] dropping a foreign envelope: ${err.message}`);
                    ch.nack(msg, false, false);
                    return;
                }
                // 处理失败不写完成态，并 requeue 交给 MQ redeliver，避免 at-least-once
                // 消息因瞬时错误或进程中断被永久吞掉；仍不 dead-letter 回 prod（§4.6）。
                console.error(`[inbound-lane] consume ${queue} error:`, err);
                ch.nack(msg, false, true);
            }
        });
        console.info(`[inbound-lane] consuming ${queue} (lane=${lane})`);
    }
}
