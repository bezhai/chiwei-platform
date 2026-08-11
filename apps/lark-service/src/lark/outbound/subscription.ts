// 出站消费的订阅生命周期：订不订、订哪几条、翻回去怎么交还。
//
// 这一层不认识飞书，也不认识队列里装的是什么 —— 队列里的消息怎么处理由每条 binding
// 自己带（见 response-queue.ts / recall-queue.ts）。
//
// ## 为什么两条队列共用一把开关
//
// 释放侧就是共用的：channel-server 的 chat-response-worker 和 recall-worker 各构造
// 一个 OutboundSubscriptions，但两者的 loadChannels 读的是**同一把** Dynamic Config
// key，所以运维把 lark 从那份清单里摘掉时，`chat_response_lark` 和 `recall_lark` 是
// 同时被释放的。接管侧要是用两把独立开关，就会出现「只翻了一把，另一条队列没有任何
// 消费者」的窗口 —— 而那个窗口是安静的：队列在涨，两个服务全部健康。
//
// 一把开关同时接两个队列，才和释放侧对称。
//
// ## 为什么不能在启动时读一次开关
//
// 决策九的交接是「旧消费者 drain → 新消费者接手」，中间既不许重叠也不许有空窗。
// 如果订阅面由启动时机决定，两条路都走不通：
//
//   先起 lark-outbound   它立刻开始消费，和还没 drain 的 channel-server 抢同一条
//                        队列，RabbitMQ 轮询把消息随机劈成两半
//   先 drain 再起        drain 完成到新进程连上 PG / bot 目录 / Redis / MQ 并开始
//                        消费之间是空窗。**泳道队列带 10s TTL**，这段初始化时延足够
//                        让消息被 DLX 弹回 prod —— 泳道的回复从 prod 实例发出去
//
// 所以正确形态是进程先起来、依赖全部预热好、但**不订阅**；等开关翻开之后原地开始
// 消费。回滚方向同理：翻回去时走 drain 屏障把队列交还，也不重启。
//
// 定时调 reconcile 的是进程入口（outbound.ts），这里只负责"读一次开关，按结果增订
// 或移交"这一件事。

import type { ConsumeMessage } from 'amqplib';
import { DynamicConfig } from '@inner/shared';
import { laneQueue, type Route } from '@inner/shared/mq';

/**
 * "本服务是否消费飞书的出站队列"。**默认关，且一把管两条。**
 *
 * 镜像可以先上线、Deployment 可以先起来、日志可以先看着 —— 什么时候真的接管出站
 * 是切换动作的一部分（Task E），跟代码发布解耦。
 *
 * 走 Dynamic Config 而不是 env：Release env 会被部署的 POST 清空，长期开关放那里
 * 会在某次部署之后悄悄失效，而这个开关失效的表现是"回复被两个服务各发一半"。
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

/** 订阅本身要用到的 MQ 表面，就这三件事。收发消息那两个由各 binding 自己带。 */
export interface OutboundSubscriptionPort {
    /** 声明队列与绑定。订一条没声明的队列等于守着空气。 */
    declareRoute(route: Route): Promise<void>;
    consume(queue: string, handler: (msg: ConsumeMessage) => Promise<void>): Promise<void>;
    /** 交接屏障：basic.cancel → 等在途归零 → 才算真的把队列交出去。 */
    drainConsumer(queue: string): Promise<void>;
}

/** 一条要订的队列，以及它的消息怎么处理。 */
export interface OutboundQueueBinding {
    /** 渠道分区后的基础路由。泳道后缀由本文件统一加。 */
    route: Route;
    /**
     * 造一个 handler。队列名传进去是给日志用的 —— 出问题时得知道是哪条队列上的
     * 哪条消息，而 handler 自己看不见队列名。
     */
    handler(queue: string): (msg: ConsumeMessage) => Promise<void>;
}

export interface LarkOutboundSubscriptionsDeps {
    amqp: OutboundSubscriptionPort;
    /**
     * 本进程所在的泳道。
     *
     * **必须与 declareRoute 用的是同一个来源**（生产上两边都读 env 的 LANE）：
     * 声明的是 A、订阅的是 B 的话，声明成功、订阅也成功，就是一条消息都收不到。
     */
    lane?: string;
    queues: OutboundQueueBinding[];
    /** 开关的原始值。见 larkOutboundConsumeSwitch。 */
    readConsumeSwitch(): Promise<string>;
}

/**
 * 出站队列的订阅，**开关翻动时原地生效，不重启进程**。
 */
export class LarkOutboundSubscriptions {
    private readonly deps: LarkOutboundSubscriptionsDeps;
    /**
     * 当前订着的队列名。**逐条记账**，不是一个"开着没开着"的布尔 —— 订一半失败时，
     * 整体记账会让没订上的那条永远补不回来，而它的表现是"那条队列默默没人消费"。
     */
    private readonly subscribed = new Set<string>();
    /** reconcile 自己的互斥：drain 最坏要等 60 秒，比定时器间隔长。 */
    private inFlight: Promise<void> | null = null;

    constructor(deps: LarkOutboundSubscriptionsDeps) {
        this.deps = deps;
    }

    subscribedQueues(): string[] {
        return [...this.subscribed];
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
        for (const binding of this.deps.queues) {
            const queue = laneQueue(binding.route.queue, this.deps.lane);
            if (wanted === this.subscribed.has(queue)) continue;
            if (wanted) await this.subscribe(binding, queue);
            else await this.handOff(queue);
        }
    }

    /**
     * 这一刻该不该消费。**没拿到有效指令就保持上次的结论**。
     *
     * 初始结论是"不消费"，所以"读不到"在启动时等于关着 —— 绝不会自己变宽到跟
     * channel-server 抢队列。而已经接管之后读不到，保持消费才是对的：擅自停下来
     * 会造成一个没人告警的静默断流。
     */
    private async wanted(): Promise<boolean> {
        const keeping = (): string =>
            this.subscribed.size > 0 ? `consuming ${this.subscribedQueues().join(', ')}` : 'off';

        let raw: string;
        try {
            raw = await this.deps.readConsumeSwitch();
        } catch (error) {
            console.error(
                `[lark-outbound] lark_outbound_switch_unavailable: could not read ` +
                    `${LARK_OUTBOUND_CONSUME_FLAG}; keeping ${keeping()}:`,
                error,
            );
            return this.subscribed.size > 0;
        }

        const parsed = parseConsumeSwitch(raw);
        if (parsed === null) {
            console.warn(
                `[lark-outbound] lark_outbound_switch_unavailable: ` +
                    `${LARK_OUTBOUND_CONSUME_FLAG} is unset or unreadable ("${raw}"); ` +
                    `keeping ${keeping()}`,
            );
            return this.subscribed.size > 0;
        }
        return parsed;
    }

    private async subscribe(binding: OutboundQueueBinding, queue: string): Promise<void> {
        await this.deps.amqp.declareRoute(binding.route);
        await this.deps.amqp.consume(queue, binding.handler(queue));
        // 订上之后才记账：declare / consume 抛错时下一次 reconcile 会重来。
        this.subscribed.add(queue);
        console.info(`[lark-outbound] consuming ${queue}`);
    }

    private async handOff(queue: string): Promise<void> {
        // 先 drain 再记账：drain 抛错说明队列还可能在投递，此时清掉记账等于自己
        // 骗自己已经交出去了。
        await this.deps.amqp.drainConsumer(queue);
        this.subscribed.delete(queue);
        console.info(`[lark-outbound] handed ${queue} back (switch is off)`);
    }
}
