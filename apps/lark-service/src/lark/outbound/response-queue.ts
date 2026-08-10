// 出站入口：飞书那条 `chat_response` 队列。
//
// 本文件只管四件事 —— 订不订、订哪条、这条消息归不归我、ACK 还是拒绝。真正把话
// 送到飞书的是 deliver.ts，它不认识 RabbitMQ。
//
// ## 只订分区队列，不订老队列
//
// 换队列的协议是「消费侧先双订阅 → 切生产者 → 旧队列排空 → drain 屏障移交」，但
// **双订阅那一步归 channel-server**：它守着不带 channel 维度的老 `chat_response`，
// 直到窗口关闭。本服务只订 `chat_response_lark{_lane}`，一条崭新的、按所有权维度
// 分好区的队列。本服务去订老队列的话，两个消费者会在同一条队列上分摊消息 ——
// 而那正是分区要杀掉的东西。
//
// ## 拿到不是飞书的消息：拒绝，而且拒绝得很凶
//
// 共库方案下 `common_agent_response` 没有 channel 列，DB 层拦不住越界写入，隔离
// 完全依赖「生产者的 rk 分对了」。rk 配错是配置问题，这道校验让它立刻暴露，而不是
// 静默写脏另一个服务的台账。
//
// 拒绝用 `nack(requeue=false)`：requeue 会让两个服务把同一条消息推来推去，压成
// 活锁；prod 队列挂着 DLX，丢进 dead_letters 还能查、能重放。
//
// **没写 channel 的 payload 也拒绝**，这一点跟 channel-server 那侧不同，是刻意的：
// 那边守的是老队列，老信封确实没有 channel 字段，兜底成飞书是对历史的兼容；这条
// 分区队列是新建的，而它唯一的生产者（agent-service 的 sink_dispatch）在 payload
// 没有 channel 时**拒绝发布**。所以这里的"没有 channel"不是历史，是异常。
//
// ## ACK 策略：只有两种情况不 ACK
//
//   1. JSON 解析不了 —— 退回去也还是解析不了，丢进 DLQ
//   2. deliver 往外抛 —— 只有台账那一次读会往外抛，它发生在任何副作用之前
//
// 其余一律 ACK，**包括发送失败和落库失败**。这是刻意的：重投会让"已经发出去一半
// 的分段消息"再发一遍，用户看到两条。代价是发不出去的消息就真的没了，只剩一行
// error 日志（deliver.ts 的 chat_response_outbound_failed）。

import type { ConsumeMessage } from 'amqplib';
import { DynamicConfig } from '@inner/shared';
import { CHAT_RESPONSE, channelRoute, laneQueue, type Route } from '@inner/shared/mq';
import { laneFromMessage } from '@inner/shared/mq-context';

import { LARK_CHANNEL } from '../channel';
import type { LarkChatResponse } from './chat-response';

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
 * "本服务是否消费飞书出站队列"。**默认关。**
 *
 * 镜像可以先上线、Deployment 可以先起来、日志可以先看着 —— 什么时候真的接管出站
 * 是切换动作的一部分（Task E），跟代码发布解耦。
 *
 * 走 Dynamic Config 而不是 env：Release env 会被部署的 POST 清空，长期开关放那里
 * 会在某次部署之后悄悄失效，而这个开关失效的表现是"回复被两个服务各发一半"。
 *
 * 只在启动时读一次，因为订阅本身是启动动作。翻开关之后要重启这个 Deployment。
 *
 * 启动时没有请求上下文，所以 Dynamic Config 按 **prod** 解析 —— 它是一个全局开关，
 * 不是按泳道分别打开的旋钮。给某条泳道单独配这个 key 不会生效。
 */
export const LARK_OUTBOUND_CONSUME_FLAG = 'enable_lark_outbound_consume';

const dynamicConfig = new DynamicConfig();

export function larkOutboundConsumeEnabled(): Promise<boolean> {
    return dynamicConfig.getBool(LARK_OUTBOUND_CONSUME_FLAG, false);
}

/** 消费者用到的 MQ 表面，就这四件事。 */
export interface LarkResponseChannel {
    /** 声明队列与绑定。订一条没声明的队列等于守着空气。 */
    declareRoute(route: Route): Promise<void>;
    consume(queue: string, handler: (msg: ConsumeMessage) => Promise<void>): Promise<void>;
    ack(msg: ConsumeMessage): void;
    nack(msg: ConsumeMessage, requeue: boolean): void;
}

export interface LarkResponseConsumerDeps {
    amqp: LarkResponseChannel;
    /**
     * 本进程所在的泳道。
     *
     * **必须与 declareRoute 用的是同一个来源**（生产上两边都读 env 的 LANE）：
     * 声明的是 A、订阅的是 B 的话，声明成功、订阅也成功，就是一条消息都收不到。
     */
    lane?: string;
    /** 把这一段送到飞书。见 deliver.ts。 */
    deliver(response: LarkChatResponse, lane?: string): Promise<void>;
    consumeEnabled(): Promise<boolean>;
    observeQueueDelay(seconds: number): void;
    now?(): number;
}

/** 订阅成功时返回队列名，开关关着时返回 null。 */
export async function startLarkResponseConsumer(
    deps: LarkResponseConsumerDeps,
): Promise<string | null> {
    if (!(await enabled(deps.consumeEnabled))) {
        console.warn(
            `[lark-outbound] ${LARK_OUTBOUND_CONSUME_FLAG} is off; not consuming ` +
                `${larkChatResponseQueue(deps.lane)} — flip it in Dynamic Config and restart`,
        );
        return null;
    }

    const route = larkChatResponseRoute();
    await deps.amqp.declareRoute(route);
    const queue = larkChatResponseQueue(deps.lane);
    const now = deps.now ?? Date.now;

    await deps.amqp.consume(queue, async (msg) => {
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
            // 走到这里只有一种可能：deliver 在做出任何副作用之前就失败了（台账那
            // 次读）。此时消息还没发出去，丢进 DLQ 可查可重放。
            console.error(`[lark-outbound] ${queue} failed before any side effect:`, error);
            deps.amqp.nack(msg, false);
            return;
        }

        deps.amqp.ack(msg);
    });

    console.info(`[lark-outbound] consuming ${queue}`);
    return queue;
}

/**
 * 读开关，**读不到一律当关**。
 *
 * DynamicConfig 自己会把 fetch 失败吞成默认值，但读取方也可能因为别的原因抛。
 * 两种情况的处置完全一样：这次没拿到有效指令，就不许自己变宽。变宽的后果是静默的
 * ——cutover 窗口里 channel-server 仍然订阅着同一条飞书队列，两个消费者守着它，
 * RabbitMQ 轮询把回复随机劈成两半，不报错、不留痕。
 */
async function enabled(read: () => Promise<boolean>): Promise<boolean> {
    try {
        return await read();
    } catch (error) {
        console.error(
            `[lark-outbound] could not read ${LARK_OUTBOUND_CONSUME_FLAG}; ` +
                'staying off (widening would make two services share one queue):',
            error,
        );
        return false;
    }
}
