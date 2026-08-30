// 出站消费的订阅生命周期：订不订、订哪几条、翻回去怎么交还。
//
// 这一层不认识飞书，也不认识队列里装的是什么 —— 队列里的消息怎么处理由每条 binding
// 自己带（见 response-queue.ts / recall-queue.ts）。
//
// ## 为什么两条队列共用一把开关
//
// 两条队列的消费者是同一个进程，开关表达的是"这个进程接不接管飞书的出站"这一件事。
// 拆成两把独立开关就会出现「只翻了一把，另一条队列没有任何消费者」的窗口 —— 而那个
// 窗口是安静的：队列在涨，进程健康，没有任何报错。
//
// 关掉它现在等于"这两条队列没有任何消费者"：飞书的出站已经全归本服务，没有别的服务
// 会接手。
//
// ## 为什么不能在启动时读一次开关
//
// 开关翻动要能在运行期生效，两个方向都不重启进程：
//
//   翻开     进程先起来、依赖（PG / bot 目录 / Redis / MQ）全部预热好、但**不订阅**；
//            开关翻开之后原地开始消费。要是靠重启来订阅，从进程启动到真正开始消费之间
//            是空窗，而**泳道队列带 10s TTL**，这段初始化时延足够让消息被 DLX 弹回
//            prod —— 泳道的回复从 prod 实例发出去
//   翻回去   走 drain 屏障（basic.cancel → 等在途归零）停止消费，同样不重启。直接杀
//            进程会让已经调完飞书 API、还没 ACK 的那条消息 requeue 再发一次，真人看到两条
//
// 定时调 reconcile 的是进程入口（outbound.ts），这里只负责"读一次开关，按结果增订
// 或停止消费"这一件事。

import type { ConsumeMessage } from 'amqplib';
import { DynamicConfig } from '@inner/shared';
import { laneQueue, type Route } from '@inner/shared/mq';

/**
 * "本服务是否消费飞书的出站队列"。**默认关，且一把管两条。**
 *
 * 镜像可以先上线、Deployment 可以先起来、日志可以先看着 —— 什么时候真的开始消费出站
 * 队列由这把开关单独决定，跟代码发布解耦。
 *
 * 走 Dynamic Config 而不是 env：Release env 会被部署的 POST 清空，长期开关放那里
 * 会在某次部署之后悄悄失效，而这个开关失效的表现是"飞书回复没有任何消费者"——
 * 队列在涨，进程健康，没有任何报错。
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
 * 的动作是不动 —— 既不擅自开（开关是不是真的该开只有操作者知道），也不擅自关
 * （本服务正在消费的话，一次配置抖动就让飞书回复没人消费，泳道队列 10 秒后被 DLX
 * 弹回 prod，泳道的回复从 prod 实例发出去）。
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
 * 一条队列在本进程眼里的状态。
 *
 * 两个 `mayBe*` 不是"中间态"，是操作抛错之后**唯一诚实的结论** —— 真实 MQ 端口的
 * 两个入口都是「先产生副作用、再可能失败」，抛错既不证明副作用没发生，也不证明它
 * 做完了。所以出错一律按**副作用已经发生**记：
 *
 *   mayBeConsuming  consume 抛错。订阅项已经躺在端口的重连恢复列表里且可恢复 ——
 *                   断线重连会把它订回来。记成"没订上"的话，开关翻回去时看不出
 *                   diff，那个消费者永远不会被 cancel，交接完成之后还在分摊消息。
 *   mayBeReleased   drainConsumer 抛错。basic.cancel 早就发出去了，broker 侧已经
 *                   不投递。记成"还订着"的话，开关翻回来时同样看不出 diff，这条
 *                   队列从此没有任何消费者 —— 没有错误、没有告警，消息在里面堆着。
 */
type QueueState = 'consuming' | 'released' | 'mayBeConsuming' | 'mayBeReleased';

/**
 * 出站队列的订阅，**开关翻动时原地生效，不重启进程**。
 */
export class LarkOutboundSubscriptions {
    private readonly deps: LarkOutboundSubscriptionsDeps;
    /**
     * 每条队列各记一笔。**逐条记账**，不是一个"开着没开着"的布尔 —— 订一半失败时，
     * 整体记账会让没订上的那条永远补不回来，而它的表现是"那条队列默默没人消费"。
     *
     * 这本账只记"队列上有没有消费者"，**不记"上次开关是什么结论"**（那是
     * lastDecision）。两者混成一个状态的话，"首次翻开就订阅失败"会被读成"上次是
     * 关的"，于是开关明明开着却再也不重试。
     */
    private readonly state = new Map<string, QueueState>();
    /**
     * 上一次**读到有效指令**时的结论。初始 false：冷启动等价于关，一次都没读到配置
     * 就不会自己开始消费。读不到时保持的就是它，跟订着几条无关。
     */
    private lastDecision = false;
    /** reconcile 自己的互斥：drain 最坏要等 60 秒，比定时器间隔长。 */
    private inFlight: Promise<void> | null = null;

    constructor(deps: LarkOutboundSubscriptionsDeps) {
        this.deps = deps;
    }

    /** 确认订上了的那些。`mayBe*` 不算 —— 对外只报有把握的。 */
    subscribedQueues(): string[] {
        return [...this.state]
            .filter(([, state]) => state === 'consuming')
            .map(([queue]) => queue);
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

    /**
     * 把每条队列推到开关要求的样子。**四种状态两个方向的行为表**：
     *
     * | 状态           | 开关开                        | 开关关 |
     * |----------------|-------------------------------|--------|
     * | consuming      | —                             | drain  |
     * | released       | subscribe                     | —      |
     * | mayBeConsuming | **先 drain 再 subscribe**     | drain  |
     * | mayBeReleased  | subscribe                     | drain  |
     *
     * mayBeConsuming 的开方向是唯一一个复合动作，因为它是唯一一个"可能有一个我不
     * 知道 tag 的消费者"的状态：端口的重连恢复会把它订回来并写上新 tag，此时直接
     * 再 consume 一次就是 broker 上两个消费者，而旧那个的 tag 已经被覆盖、再也
     * cancel 不掉。先 drain 一次把它摘干净，才谈得上"正好一个消费者"。
     *
     * 反过来 mayBeReleased 的开方向可以直接 subscribe：basic.cancel 已经发过了，
     * 端口那一项复用之后重新注册，落地就是正好一个。这里**不能**也走 drain ——
     * drain 抛错本来就多半是在途 handler 卡住，再 drain 一次只会继续卡着，而那时
     * 开关已经要求恢复消费了。
     */
    private async settle(): Promise<void> {
        const wanted = await this.wanted();
        for (const binding of this.deps.queues) {
            const queue = laneQueue(binding.route.queue, this.deps.lane);
            const state = this.state.get(queue) ?? 'released';
            if (wanted) {
                if (state === 'consuming') continue;
                if (state === 'mayBeConsuming') await this.handOff(queue);
                await this.subscribe(binding, queue);
            } else {
                if (state === 'released') continue;
                await this.handOff(queue);
            }
        }
    }

    /**
     * 这一刻该不该消费。**没拿到有效指令就保持上次的结论**。
     *
     * 初始结论是"不消费"，所以"读不到"在启动时等于关着 —— 一次都没读到配置就不会
     * 自己开始消费。而已经在消费之后读不到，保持消费才是对的：擅自停下来会造成一个
     * 没人告警的静默断流。
     */
    private async wanted(): Promise<boolean> {
        // 报的是"上次的结论"本身，不是"现在订着几条" —— 订阅失败之后这两者会分家，
        // 而分家的那一刻正是最需要看懂这行日志的时候。
        const keeping = (): string => {
            if (!this.lastDecision) return 'off';
            const queues = this.subscribedQueues();
            return `on (consuming ${queues.join(', ') || 'nothing yet'})`;
        };

        let raw: string;
        try {
            raw = await this.deps.readConsumeSwitch();
        } catch (error) {
            console.error(
                `[lark-outbound] lark_outbound_switch_unavailable: could not read ` +
                    `${LARK_OUTBOUND_CONSUME_FLAG}; keeping ${keeping()}:`,
                error,
            );
            return this.lastDecision;
        }

        const parsed = parseConsumeSwitch(raw);
        if (parsed === null) {
            console.warn(
                `[lark-outbound] lark_outbound_switch_unavailable: ` +
                    `${LARK_OUTBOUND_CONSUME_FLAG} is unset or unreadable ("${raw}"); ` +
                    `keeping ${keeping()}`,
            );
            return this.lastDecision;
        }
        this.lastDecision = parsed;
        return parsed;
    }

    private async subscribe(binding: OutboundQueueBinding, queue: string): Promise<void> {
        // 记账先于副作用：consume 抛错时订阅项已经在端口的重连恢复列表里了，抛完
        // 再记就是记了个假账。
        this.state.set(queue, 'mayBeConsuming');
        await this.deps.amqp.declareRoute(binding.route);
        await this.deps.amqp.consume(queue, binding.handler(queue));
        this.state.set(queue, 'consuming');
        console.info(`[lark-outbound] consuming ${queue}`);
    }

    private async handOff(queue: string): Promise<void> {
        // 同理：drainConsumer 先发 basic.cancel 再等在途归零，超时抛错时 broker 侧
        // 已经取消了。
        this.state.set(queue, 'mayBeReleased');
        await this.deps.amqp.drainConsumer(queue);
        this.state.set(queue, 'released');
        console.info(`[lark-outbound] handed ${queue} back`);
    }
}
