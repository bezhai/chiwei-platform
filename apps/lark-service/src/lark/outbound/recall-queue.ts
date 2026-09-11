// 出站入口二：飞书那条 `recall` 队列。
//
// 本文件只管三件事 —— 订哪条队列、这条消息归不归我、处理完之后 ACK / 退回 / 重投。
// 撤哪几条、算不算成功、台账写什么在 recall.ts，它不认识 RabbitMQ；什么时候订、什么
// 时候交还在 subscription.ts，它不认识飞书（跟 `chat_response` 共用，也共用同一把
// 开关）。
//
// ## 拿到不是飞书的消息：拒绝，而且拒绝得很凶
//
// 撤回写的是 `common_agent_response` 的 safety_status / safety_result —— 写入矩阵里
// 那处字段级重叠（agent-service 的安全判定与撤回链路双向写），而这张表没有 channel
// 列，DB 层拦不住越界。隔离完全依赖「生产者的 rk 分对了」，这道校验让 rk 配错立刻
// 暴露，而不是静默写脏另一个服务的台账。
//
// 拒绝用 `nack(requeue=false)`：requeue 只是把消息原样退回这条队列，下一轮还是本进程
// 拿到，同一条消息在这里转圈；prod 队列挂着 DLX，丢进 dead_letters 还能查、能重放。
//
// **没写 channel 的 payload 一样拒绝，不兜底成飞书**：撤回也是 channel 分区的队列，
// agent-service 那侧同样用 `channel_route_for_payload` 算 rk，payload 缺 channel 时
// 直接抛、发不出来。所以真收到一条，只可能是有人绕过了那条路径，兜底等于把分流错误
// 变成静默错投。
//
// ## 整条处理都跑在入站消息的上下文里
//
// 不是为了日志好看：重投走 publish，而 publish 的 trace_id 取自 AsyncLocalStorage ——
// 重投分支跑在 context 之外时写进 header 的就是空串，真实重试路径上 trace 链断掉。
// 显式往 headers 里塞 trace_id 也没用：publish 内部的权威值会盖掉调用方给的。

import type { ConsumeMessage } from 'amqplib';
import { context } from '@inner/shared/middleware';
import { channelRoute, laneQueue, RECALL, type Route } from '@inner/shared/mq';
import { laneFromMessage, traceIdFromMessage } from '@inner/shared/mq-context';

import { LARK_CHANNEL } from '../channel';
import type { LarkRecallOutcome, LarkRecallPayload, LarkRecallRequest } from './recall';
import type { OutboundQueueBinding } from './subscription';

/**
 * 飞书撤回的 Route。
 *
 * 名字由共享包的 channelRoute 拼，本文件一个字面量都不写 —— 队列名是**跨语言契约**，
 * 生产者在 Python 那边。测试把它接到 contracts/mq-channel-routes.json 上。
 */
export function larkRecallRoute(): Route {
    return channelRoute(RECALL, LARK_CHANNEL);
}

/** 本进程该订的那条队列。lane 缺省即 prod。 */
export function larkRecallQueue(lane?: string): string {
    return laneQueue(larkRecallRoute().queue, lane);
}

/**
 * 重投次数写在这个 AMQP header 上。
 *
 * 读和写都在本文件：换个名字会让"已经重投过三次"的消息重新从 0 开始，无限重投。
 */
export const RECALL_RETRY_HEADER = 'x-retry-count';

/** 消费这条队列要用到的 MQ 表面。订阅本身在 subscription.ts。 */
export interface LarkRecallChannel {
    ack(msg: ConsumeMessage): void;
    nack(msg: ConsumeMessage, requeue: boolean): void;
    /** 延时重投。route 由本文件给，投回的必须是**同 channel** 的那条队列。 */
    publish(
        route: Route,
        body: Record<string, unknown>,
        delayMs?: number,
        headers?: Record<string, unknown>,
        lane?: string,
    ): Promise<void>;
}

export interface LarkRecallConsumerDeps {
    amqp: LarkRecallChannel;
    /** 撤这一条。见 recall.ts。 */
    recall(request: LarkRecallRequest): Promise<LarkRecallOutcome>;
}

/** 入站消息已经被重投过几次。不是数字就当没重投过。 */
function retryCountOf(msg: ConsumeMessage): number {
    const raw = msg.properties.headers?.[RECALL_RETRY_HEADER];
    return typeof raw === 'number' && Number.isFinite(raw) ? raw : 0;
}

/** 飞书 `recall` 这条队列的订阅项。泳道后缀由 subscription.ts 统一加。 */
export function larkRecallBinding(deps: LarkRecallConsumerDeps): OutboundQueueBinding {
    return {
        route: larkRecallRoute(),
        handler: (queue) => async (msg) => {
            let payload: LarkRecallPayload;
            try {
                payload = JSON.parse(msg.content.toString()) as LarkRecallPayload;
            } catch {
                console.error(
                    `[lark-outbound] malformed message on ${queue}, sending to DLQ: ` +
                        msg.content.toString().slice(0, 200),
                );
                deps.amqp.nack(msg, false);
                return;
            }

            // 归属判断必须先于任何副作用：一行库不查、一个飞书 API 不调。
            if (payload.channel !== LARK_CHANNEL) {
                // 稳定的 event 名，make logs KEYWORD=recall_foreign_channel 可捞。
                console.error(
                    JSON.stringify({
                        event: 'recall_foreign_channel',
                        queue,
                        channel: payload.channel ?? null,
                        // 两种定位方式都记：一条撤回只带其中一个，只记 session_id
                        // 的话，她自己开口那条链上的越界消息全长成 null，捞出来也不
                        // 知道是哪一句。
                        session_id: payload.session_id ?? null,
                        outbound_id: payload.outbound_id ?? null,
                        consumer_tag: msg.fields?.consumerTag ?? null,
                    }),
                );
                deps.amqp.nack(msg, false);
                return;
            }

            const lane = laneFromMessage(msg);
            // 入站没带 trace_id 时 createContext 生成一个，业务层复用它这个**生成后**
            // 的值，否则内外两层会是两条不同的 trace。
            const inbound = context.createContext(traceIdFromMessage(msg), { lane });
            await context.run(inbound, async () => {
                const outcome = await deps.recall({
                    payload,
                    retryCount: retryCountOf(msg),
                    lane,
                    traceId: inbound.traceId,
                });

                if (outcome.kind === 'retry') {
                    // 随行 header 只写重试计数：lane header 由 publish 按下面这个 lane
                    // 参数统一注入（连同 trace_id），调用点不重复写一份。
                    //
                    // 没有 lane 就**显式**投回 prod —— 传 undefined 会让 publish 回落
                    // 进程 env LANE，prod 实例接手降级消息时那正是要命的误判。
                    await deps.amqp.publish(
                        larkRecallRoute(),
                        payload as unknown as Record<string, unknown>,
                        outcome.delayMs,
                        { [RECALL_RETRY_HEADER]: outcome.retryCount },
                        lane ?? 'prod',
                    );
                    deps.amqp.ack(msg);
                    return;
                }

                if (outcome.kind === 'exhausted') {
                    // 终态已经写成 recall_failed，剩下的只是让这条消息留个痕。
                    deps.amqp.nack(msg, false);
                    return;
                }

                deps.amqp.ack(msg);
            });
        },
    };
}
