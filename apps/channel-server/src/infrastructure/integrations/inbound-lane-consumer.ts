// 入站 lane 消费者（lane-routing-redesign §4.3/§4.4）。lane channel-server 起这个
// 消费者，消费自己 lane 的信封队列。当前重放完整入站链（含 MessageTransferer、识图
// 管线、common bot presence upsert 等前置副作用）；抽出真正后半段是已知待办，本文件
// 暂不改变行为。
//
// 队列声明走 fail-closed 的 assertInboundLaneQueue（无 10s TTL、无 DLX 回 prod，§4.6）。
//
// 幂等（§4.4 point 5）：channel + event_type + globalMessageId + lane 唯一确定"这条
// 事件在这条泳道处理了一次"。协议是原子占位（claim → 处理 → complete），实现只有一份，
// 在 @inner/shared/inbound-lane-claim —— 共享队列上同一条信封可能被本服务拿到、也可能被
// lark-service 拿到，协议不一样就会一边丢消息、一边重复处理。key 不含队列名，所以同一条
// 信封走共享队列还是分区队列都只处理一次。
//
// ## 两条队列，因为所有权正在从 lane 迁到 channel + lane
//
// 分区前的 inbound_lane.{lane} 只按 lane 分，但 owner 是 channel + lane：飞书的信封
// 归 lark-service，QQ 的归本服务，两个服务却在竞争消费同一条队列。换队列名不可能原子
// 发布（生产者和消费者在不同的 Deployment 里），所以走「消费侧先订上新队列 → 切生产者
// → 旧队列排空 → 移交」，本文件承担头一步。
//
// **这场迁移还停在头一步**：enable_inbound_lane_channel_consume / _publish 两个 flag
// 在 prod 都没建（= 默认关），所以泳道信封目前全部走共享队列，分区队列只在打开开关的
// 环境里才被订上。共享队列的订阅因此无条件保留。
//
// ## 分区不等于不再竞争，还得有人真的不去收
//
// 移交一个 channel 的时刻和删掉它的代码的时刻必然不同，中间那段时间"订阅面跟着 runtime
// 走"意味着两个服务都守着同一条分区队列 —— 分区等于没做。移交因此是一个运行期的显式
// 动作：拥有集合走 dynamic config（inbound-lane-channels.ts），订阅面按启动快照，
// **认领**逐条现读。
//
// 于是一条信封要过三关：
//
//   分区队列的归属断言   分区之后这条队列里只该有对应 channel 的信封；破了说明投递侧
//                        发错了队列，而这条队列没有第二个消费者——退回去只会永远弹，
//                        prefetch=1 会让它把整条泳道堵死。所以丢，但要吼出来
//   拥有集合             不在集合里就退回去（两种队列上都一样），等它真正的 owner 拿。
//                        自己处理掉 = 流量被随机劈成两半，不报错、不留痕
//   原子占位             同一条消息只有一个消费者能占到位

import { inboundLaneClaims, type InboundLaneClaims } from '@inner/shared/inbound-lane-claim';
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
import { isInboundLaneChannelConsumeEnabled } from './inbound-lane-flag';
import { context } from '@middleware/context';

export interface ConsumeDeps {
    // 幂等占位。协议（key、值、租约、状态机）是跨服务契约，见
    // @inner/shared/inbound-lane-claim。
    claims: InboundLaneClaims;
    // 入站处理。
    process: (env: InboundLaneEnvelope) => Promise<void>;
}

/**
 * 一条信封的下场。调用方据此决定 ACK 还是退回队列——`in-flight` **绝不能** ACK：
 * 对方还没写完成标记，ACK 会把消息销毁。
 */
export type LaneOutcome = 'handled' | 'already-done' | 'in-flight';

// 纯逻辑：注入 claims/process，确定性测幂等分叉。
export async function consumeInboundLaneEnvelope(
    env: InboundLaneEnvelope,
    deps: ConsumeDeps,
): Promise<LaneOutcome> {
    const key = inboundDedupeKey(env);
    const claim = await deps.claims.claim(key);
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
        await deps.claims.release(key).catch((releaseError) => {
            console.error(`[inbound-lane] failed to release ${key}:`, releaseError);
        });
        throw error;
    }
    await deps.claims.complete(key);
    return 'handled';
}

// 分区队列上收到别人的信封。单独一个类型，因为它的处置跟"处理失败"相反：重投一万次
// 也还是错的，只能丢。
export class ForeignEnvelope extends Error {}

// 报文根本不是合法 JSON。跟 ForeignEnvelope 同一类永久性错误：队列里的字节不会因为
// 重投而变得能解析。lark-service 那半边同一条协议上叫 Unprocessable（ingress/
// lane-queue.ts），处置一样。
class UnparseableMessage extends Error {}

// 报文 → 信封。解析失败包成 UnparseableMessage，好让每条消息的下场只在 catch 里判一次。
// 不打印报文内容：这条队列上流的是用户消息，长度和解析错误足够定位问题。
function parseEnvelope(content: Buffer, queue: string): InboundLaneEnvelope {
    try {
        return JSON.parse(content.toString()) as InboundLaneEnvelope;
    } catch (error) {
        throw new UnparseableMessage(
            `${queue} holds ${content.byteLength} bytes that are not JSON: ` +
                `${(error as Error).message}`,
        );
    }
}

// 分区之后的不变量：这条队列里只会有 queueChannel 的信封。它需要被持续保证，不是可以
// 默认的事实 —— 所以留一条断言，而不是删掉校验。
//
// 说不出自己是哪个 channel 的信封在这里也算破了不变量，处置跟"别人的 channel"一样：
// 分区队列没有第二个消费者，退回去只会永远弹，prefetch=1 会把整条泳道堵死。
export function assertEnvelopeChannel(
    env: InboundLaneEnvelope,
    queueChannel: string,
    queue: string,
): void {
    let channel: string;
    try {
        channel = envelopeChannel(env);
    } catch {
        throw new ForeignEnvelope(
            `${queue} must only ever hold "${queueChannel}" envelopes, but this one names ` +
                `no channel at all (event=${env.event_type} gmid=${env.global_message_id})`,
        );
    }
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
    // 与 handles 分开，是因为移交进行中两者必然不等：代码还在（能处理），流量已经
    // 移交出去了（不该处理）。见 inbound-lane-channels.ts。
    loadOwnedChannels?: () => Promise<string[]>;
    // 是否订阅分区队列。默认读 dynamic config，默认关。订阅是启动动作，只在这里读一次
    // ——翻开之后要重启消费者才生效。
    channelQueueEnabled?: () => Promise<boolean>;
    // 退回队列之前等多久（只有重投过的才等）。注入只为测试。
    wait?: (ms: number) => Promise<void>;
    // 幂等占位。默认走 redis，注入只为测试。
    claims?: InboundLaneClaims;
}

// 一条订阅。channel 只在分区队列上有值，它是"这条队列里只该有谁"的断言依据。
interface LaneSubscription {
    queue: string;
    channel?: string;
}

// 退回队列之前等多久。压热循环用的，不是重试退避：交接和失败重试正常都是一两次就完，
// 只有对面一直不在线、或者失败是确定性的，才会一直弹回来。
const GIVE_BACK_DELAY_MS = 1_000;

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
    const claims = options.claims ?? inboundLaneClaims;

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

            // 退回队列。只有重投过的才等一下——正常交接和瞬时失败的重试不该背延迟成本，
            // 而一直弹回来说明对面不在线（或者失败是确定性的），那就把热循环压成慢轮询。
            // 消息始终留在队列里，不丢。
            const giveBack = async (): Promise<void> => {
                if (redelivered) await wait(GIVE_BACK_DELAY_MS);
                ch.nack(msg, false, true);
            };

            try {
                const env = parseEnvelope(msg.content, queue);
                if (channel) assertEnvelopeChannel(env, channel, queue);

                // ---- 所有权判断必须先于任何认领 ----
                // 认领别人的消息 = 替对面写下"这条处理过了"，比 ACK 掉还糟。
                // 逐条现读（dynamic config 有 10s 缓存），所以移交不需要重启：把
                // channel 从拥有集合里摘掉的那一刻起，它的信封就一律退回去等对面拿。
                //
                // 走到这里只剩共享队列（分区队列在上面那条断言里已经处置完）。共享队列
                // 上说不出 channel 的信封判不了归属，跟"不是我的 channel"同一个处置：
                // 退回去等认得出它的人，别猜。
                let envChannel: string;
                try {
                    envChannel = envelopeChannel(env);
                } catch {
                    console.warn(
                        `[inbound-lane] ${queue} holds an envelope that names no channel; ` +
                            `handing it back (event=${env.event_type} ` +
                            `gmid=${env.global_message_id})`,
                    );
                    await giveBack();
                    return;
                }
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
                    claims,
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
                // 永久性错误：这条消息重投多少次都是同一个下场（别人的信封 / 根本不是
                // JSON 的报文）。prefetch=1 会把它变成热循环——退回队头、立刻又投给自己
                // ——整条泳道的后续消息永远排不上。所以丢，但要吼出来。
                if (err instanceof ForeignEnvelope || err instanceof UnparseableMessage) {
                    console.error(
                        `[inbound-lane] dropping a message from ${queue}: ${err.message}`,
                    );
                    ch.nack(msg, false, false);
                    return;
                }
                // 处理失败可能是瞬时的（下游抖动、进程被杀），所以不写完成态、requeue 交给
                // MQ redeliver，避免 at-least-once 消息被永久吞掉；仍不 dead-letter 回
                // prod（§4.6）。但退回去要走 giveBack：直接 nack 的话，确定性的失败会以
                // 队列的最高速度原地打转，一样堵死泳道。
                console.error(`[inbound-lane] consume ${queue} error:`, err);
                await giveBack();
            }
        });
        console.info(`[inbound-lane] consuming ${queue} (lane=${lane})`);
    }
}
