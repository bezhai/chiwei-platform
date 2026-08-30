// 出站入口：飞书那条 `chat_response` 队列。
//
// 本文件只管三件事 —— 订哪条队列、这条消息归不归我、ACK 还是拒绝。真正把话送到
// 飞书的是 deliver.ts，它不认识 RabbitMQ；什么时候订、什么时候交还在 subscription.ts，
// 那一层不认识飞书（`recall` 那条队列共用它，也共用同一把开关）。
//
// ## 拿到不是飞书的消息：拒绝，而且拒绝得很凶
//
// 共库方案下 `common_agent_response` 没有 channel 列，DB 层拦不住越界写入，隔离
// 完全依赖「生产者的 rk 分对了」。rk 配错是配置问题，这道校验让它立刻暴露，而不是
// 静默写脏另一个服务的台账。
//
// 拒绝用 `nack(requeue=false)`：requeue 只是把消息原样退回这条队列，下一轮还是本
// 进程拿到，同一条消息在这里转圈；prod 队列挂着 DLX，丢进 dead_letters 还能查、
// 能重放。
//
// **没写 channel 的 payload 一样拒绝，不兜底成飞书**：这里的"没有 channel"不可能是
// 历史遗留。这条队列唯一的生产者是 agent-service 的 sink_dispatch，它算 rk 走
// `channel_route_for_payload`，payload 缺 channel 时那里直接抛、压根发不出来。所以
// 真收到一条，只可能是有人绕过了那条路径，兜底等于把分流错误变成静默错投。
//
// ## ACK 策略：只有两种情况不 ACK
//
//   1. JSON 解析不了 —— 退回去也还是解析不了，丢进 DLQ
//   2. deliver 往外抛 —— 它只在**还没调过飞书 API** 时才抛（台账那次读、agent 报的
//      失败、空的收尾段、反查失败；分界线见 deliver.ts 文件头）
//
// 其余一律 ACK，**包括发送失败和落库失败**。这是刻意的：重投会让"已经发出去一半
// 的分段消息"再发一遍，用户看到两条。代价是发不出去的消息就真的没了，只剩一行
// error 日志（deliver.ts 的 chat_response_outbound_failed）。

import type { ConsumeMessage } from 'amqplib';
import { CHAT_RESPONSE, channelRoute, laneQueue, type Route } from '@inner/shared/mq';
import { laneFromMessage } from '@inner/shared/mq-context';

import { LARK_CHANNEL } from '../channel';
import type { LarkChatResponse } from './chat-response';
import type { OutboundQueueBinding } from './subscription';

/**
 * 飞书出站的 Route。
 *
 * 名字由共享包的 channelRoute 拼（channel 揉进 base 名，泳道后缀继续加在最后），
 * 本文件一个字面量都不写 —— 队列名是**跨语言契约**，生产者在 Python 那边，两边
 * 各写一份字面量就等于没有契约。测试把它接到 contracts/mq-channel-routes.json 上。
 */
export function larkChatResponseRoute(): Route {
    return channelRoute(CHAT_RESPONSE, LARK_CHANNEL);
}

/** 本进程该订的那条队列。lane 缺省即 prod。 */
export function larkChatResponseQueue(lane?: string): string {
    return laneQueue(larkChatResponseRoute().queue, lane);
}

/**
 * 消费这条队列要用到的 MQ 表面，就 ACK 这两件事。
 *
 * 订阅本身（declare / consume / drain）不在这里 —— 那是 subscription.ts 的
 * OutboundSubscriptionPort，两条出站队列共用一份实现。
 */
export interface LarkResponseChannel {
    ack(msg: ConsumeMessage): void;
    nack(msg: ConsumeMessage, requeue: boolean): void;
}

export interface LarkResponseConsumerDeps {
    amqp: LarkResponseChannel;
    /** 把这一段送到飞书。见 deliver.ts。 */
    deliver(response: LarkChatResponse, lane?: string): Promise<void>;
    observeQueueDelay(seconds: number): void;
    now?(): number;
}

/** 飞书 `chat_response` 这条队列的订阅项。泳道后缀由 subscription.ts 统一加。 */
export function larkChatResponseBinding(deps: LarkResponseConsumerDeps): OutboundQueueBinding {
    const now = deps.now ?? Date.now;

    return {
        route: larkChatResponseRoute(),
        handler: (queue) => async (msg) => {
            let response: LarkChatResponse;
            try {
                response = JSON.parse(msg.content.toString()) as LarkChatResponse;
            } catch {
                console.error(
                    `[lark-outbound] malformed message on ${queue}, sending to DLQ: ` +
                        msg.content.toString().slice(0, 200),
                );
                deps.amqp.nack(msg, false);
                return;
            }

            // 归属判断必须先于任何副作用：一行库不查、一个飞书 API 不调。
            if (response.channel !== LARK_CHANNEL) {
                // 稳定的 event 名，make logs KEYWORD=chat_response_foreign_channel 可捞。
                console.error(
                    JSON.stringify({
                        event: 'chat_response_foreign_channel',
                        queue,
                        channel: response.channel ?? null,
                        session_id: response.session_id ?? null,
                        message_id: response.message_id ?? null,
                        consumer_tag: msg.fields?.consumerTag ?? null,
                    }),
                );
                deps.amqp.nack(msg, false);
                return;
            }

            // 队列积压。没有 published_at 就不记 —— 补一个 0 会把曲线压平，
            // 而"看不出积压"正是切流时最不该有的盲区。
            if (response.published_at) {
                const delayMs = now() - response.published_at;
                if (delayMs > 0) deps.observeQueueDelay(delayMs / 1000);
            }

            try {
                await deps.deliver(response, laneFromMessage(msg));
            } catch (error) {
                // deliver 只在**还没调过飞书 API** 时才往外抛（分界线见 deliver.ts
                // 文件头）。此时消息没发出去，丢进 DLQ 可查可重放。
                console.error(`[lark-outbound] ${queue} failed before any side effect:`, error);
                deps.amqp.nack(msg, false);
                return;
            }

            deps.amqp.ack(msg);
        },
    };
}
