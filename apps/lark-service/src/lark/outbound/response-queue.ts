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
//   2. deliver 往外抛 —— 它只在**还没调过飞书 API** 时才抛（台账那次读、agent 报的
//      失败、空的收尾段、反查失败；分界线见 deliver.ts 文件头）
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
 * **运行期反复读，不是启动时读一次**：见 LarkResponseSubscription 的文件内注释 ——
 * 启动时读一次会让决策九的 drain 移交无法安全执行。
 *
 * 读的时候没有请求上下文，所以 Dynamic Config 按 **prod** 解析 —— 它是一个全局
 * 开关，不是按泳道分别打开的旋钮。给某条泳道单独配这个 key 不会生效。
 */
export const LARK_OUTBOUND_CONSUME_FLAG = 'enable_lark_outbound_consume';

const dynamicConfig = new DynamicConfig();

/**
 * 读开关的**原始值**，不在这一层兜底成 boolean。
 *
 * `DynamicConfig.getBool(key, false)` 会把三件不同的事压成同一个 false：明确配了
 * 关、压根没配、以及 fetch 失败（SDK 内部把它吞成默认值）。压平之后就没法区分
 * 「操作者说关」和「这次没拿到有效指令」——而这两者的处置必须不同，见
 * parseConsumeSwitch。
 */
export function larkOutboundConsumeSwitch(): Promise<string> {
    return dynamicConfig.get(LARK_OUTBOUND_CONSUME_FLAG, '');
}

/**
 * 开关的三种读数：明确开 / 明确关 / **没有有效指令**（null）。
 *
 * 第三种不是"关"：它可能是配置还没建，也可能是 paas-engine 不可达。这时候唯一安全
 * 的动作是不动 —— 既不擅自开（两个服务分摊同一条队列），也不擅自关（本服务已经
 * 接管的话，一次配置抖动就让飞书回复没人消费，泳道队列 10 秒后被 DLX 弹回 prod）。
 */
export function parseConsumeSwitch(raw: string): boolean | null {
    const value = raw.trim().toLowerCase();
    if (['true', '1', 'yes'].includes(value)) return true;
    if (['false', '0', 'no'].includes(value)) return false;
    return null;
}

/** 消费者用到的 MQ 表面，就这五件事。 */
export interface LarkResponseChannel {
    /** 声明队列与绑定。订一条没声明的队列等于守着空气。 */
    declareRoute(route: Route): Promise<void>;
    consume(queue: string, handler: (msg: ConsumeMessage) => Promise<void>): Promise<void>;
    /** 交接屏障：basic.cancel → 等在途归零 → 才算真的把队列交出去。 */
    drainConsumer(queue: string): Promise<void>;
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
    /** 开关的原始值。见 larkOutboundConsumeSwitch。 */
    readConsumeSwitch(): Promise<string>;
    observeQueueDelay(seconds: number): void;
    now?(): number;
}

/**
 * 出站队列的订阅，**开关翻动时原地生效，不重启进程**。
 *
 * ## 为什么不能在启动时读一次开关
 *
 * 决策九的交接是「旧消费者 drain → 新消费者接手」，中间既不许重叠也不许有空窗。
 * 如果订阅面由启动时机决定，两条路都走不通：
 *
 *   先起 lark-outbound   它立刻开始消费，和还没 drain 的 channel-server 抢同一条
 *                        队列，RabbitMQ 轮询把回复随机劈成两半
 *   先 drain 再起        drain 完成到新进程连上 PG / bot 目录 / Redis / MQ 并开始
 *                        消费之间是空窗。**泳道队列带 10s TTL**，这段初始化时延足够
 *                        让消息被 DLX 弹回 prod —— 泳道的回复从 prod 实例发出去
 *
 * 所以正确形态是进程先起来、依赖全部预热好、但**不订阅**；等开关翻开之后原地开始
 * 消费。回滚方向同理：翻回去时走 drain 屏障把队列交还，也不重启。
 *
 * 定时调 reconcile 的是进程入口（outbound.ts），这里只负责"读一次开关，按结果增订
 * 或移交"这一件事。
 */
export class LarkResponseSubscription {
    private readonly deps: LarkResponseConsumerDeps;
    /** 当前订阅着的队列。null = 没在消费，同时也是"上次的结论"。 */
    private queue: string | null = null;
    /** reconcile 自己的互斥：drain 最坏要等 60 秒，比定时器间隔长。 */
    private inFlight: Promise<void> | null = null;

    constructor(deps: LarkResponseConsumerDeps) {
        this.deps = deps;
    }

    subscribedQueue(): string | null {
        return this.queue;
    }

    /** 再读一次开关，按结果增订或移交。 */
    reconcile(): Promise<void> {
        // 定时器的间隔比 drain 的最坏耗时短，两次 reconcile 重叠是常态。不挡住的话
        // 第二次会看到"还没订上"的中间状态，把同一条队列再订一遍 —— 同一个进程里
        // 两个消费者分摊消息，prefetch 也翻倍。
        if (this.inFlight) return this.inFlight;
        this.inFlight = this.settle().finally(() => {
            this.inFlight = null;
        });
        return this.inFlight;
    }

    private async settle(): Promise<void> {
        const wanted = await this.wanted();
        if (wanted === (this.queue !== null)) return;
        if (wanted) await this.subscribe();
        else await this.handOff();
    }

    /**
     * 这一刻该不该消费。**没拿到有效指令就保持上次的结论**。
     *
     * 初始结论是"不消费"，所以"读不到"在启动时等于关着 —— 绝不会自己变宽到跟
     * channel-server 抢队列。而已经接管之后读不到，保持消费才是对的：擅自停下来
     * 会造成一个没人告警的静默断流。
     */
    private async wanted(): Promise<boolean> {
        let raw: string;
        try {
            raw = await this.deps.readConsumeSwitch();
        } catch (error) {
            console.error(
                `[lark-outbound] lark_outbound_switch_unavailable: could not read ` +
                    `${LARK_OUTBOUND_CONSUME_FLAG}; keeping ` +
                    `${this.queue ? `consuming ${this.queue}` : 'off'}:`,
                error,
            );
            return this.queue !== null;
        }

        const parsed = parseConsumeSwitch(raw);
        if (parsed === null) {
            console.warn(
                `[lark-outbound] lark_outbound_switch_unavailable: ` +
                    `${LARK_OUTBOUND_CONSUME_FLAG} is unset or unreadable ("${raw}"); keeping ` +
                    `${this.queue ? `consuming ${this.queue}` : 'off'}`,
            );
            return this.queue !== null;
        }
        return parsed;
    }

    private async subscribe(): Promise<void> {
        const route = larkChatResponseRoute();
        await this.deps.amqp.declareRoute(route);
        const queue = larkChatResponseQueue(this.deps.lane);
        await this.deps.amqp.consume(queue, this.handler(queue));
        // 订上之后才记账：declare / consume 抛错时下一次 reconcile 会重来。
        this.queue = queue;
        console.info(`[lark-outbound] consuming ${queue}`);
    }

    private async handOff(): Promise<void> {
        const queue = this.queue!;
        // 先 drain 再记账：drain 抛错说明队列还可能在投递，此时清掉记账等于自己
        // 骗自己已经交出去了。
        await this.deps.amqp.drainConsumer(queue);
        this.queue = null;
        console.info(`[lark-outbound] handed ${queue} back (switch is off)`);
    }

    private handler(queue: string): (msg: ConsumeMessage) => Promise<void> {
        const deps = this.deps;
        const now = deps.now ?? Date.now;

        return async (msg) => {
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
        };
    }
}
