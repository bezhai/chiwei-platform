// 出站消费的订阅面：本进程拥有哪些 channel，就按传进来的基础路由订哪几条
// {route}_{channel} 队列。当前唯一的调用点传的是 CHAT_RESPONSE —— 撤回归 lark-service
// 自己消费，本服务不再起 recall 消费者。
//
// 出站队列按 channel 分区，因为 owner 按 channel 拆成了两个服务。共用一条队列意味着
// RabbitMQ 轮询把回复随机劈成两半，两个服务各发一半 —— 不报错、不留痕。
//
// 把某个 channel 移交给另一个服务时顺序是：先把它从认领集合里摘掉，再对它自己的队列
// 走 drain 屏障（basic.cancel → 等在途归零）。反过来做会留下一个「已经不订阅了、但还
// 认领」的窗口。移交必须是 drain 而不是「停旧起新」：已经调完平台 API、还没 ACK 的那
// 一瞬间被杀，消息 requeue 换个消费者再发一次，真人看到两条。
//
// 认领集合和「哪条队列已经订上」是**两本账**。合成一本的话，认领集合既要在 drain
// 之前提交（上面那条顺序），又是 diff 的唯一来源 —— 于是 subscribe / drain 任何一步
// 抛错，下一次算出来的 diff 就是空的，失败被永久固化：要么那条 channel 的队列从此
// 没有消费者，要么它一直挂着一个本该交出去的消费者。两本账分开之后，认领照旧先提交，
// 订阅账只在真的订上 / 真的排空之后才动，失败下一次就会重做。

import { channelRoute, laneQueue, type MessageHandler, type Route } from '@inner/shared/mq';

export interface OutboundSubscriptionPort {
    declareRoute(route: Route): Promise<void>;
    consume(queue: string, handler: MessageHandler): Promise<void>;
    drainConsumer(queue: string): Promise<void>;
}

export interface OutboundSubscriptionsOptions {
    /** 渠道无关的基础路由（当前只有 CHAT_RESPONSE）。 */
    base: Route;
    lane?: string;
    port: OutboundSubscriptionPort;
    /**
     * 造一个 handler，它只处理 accepts 放行的 channel。每条 channel 队列拿到的都是
     * 「只认自己那个」——队列绑定和 payload 打架时以队列为准。
     */
    handlerFor: (accepts: (channel: string) => boolean) => MessageHandler;
    loadChannels: () => Promise<string[]>;
}

/**
 * 一条 channel 队列在本进程眼里的状态。
 *
 * 两个 `mayBe*` 是操作抛错之后唯一诚实的结论：真实 MQ 端口的两个入口都是「先产生
 * 副作用、再可能失败」，所以出错一律按**副作用已经发生**记。
 *
 *   mayBeConsuming  consume 抛错。订阅项已经在端口的重连恢复列表里，断线重连会把
 *                   它订回来 —— 记成"没订上"的话，移交时看不出 diff，那个消费者
 *                   永远不会被 cancel，会活过交接跟接手方分摊消息。
 *   mayBeReleased   drainConsumer 抛错。basic.cancel 已经发出去了，broker 侧不再
 *                   投递 —— 记成"还订着"的话，回滚时同样看不出 diff，这条队列从此
 *                   没有任何消费者。
 */
type QueueState = 'consuming' | 'released' | 'mayBeConsuming' | 'mayBeReleased';

export class OutboundSubscriptions {
    private readonly options: OutboundSubscriptionsOptions;
    /** 认领集合：该订哪几条队列的依据，移交时**先于 drain** 提交。 */
    private owned = new Set<string>();
    /** 订阅账，按队列名记。跟认领集合分开的理由见文件头。 */
    private readonly state = new Map<string, QueueState>();

    constructor(options: OutboundSubscriptionsOptions) {
        this.options = options;
    }

    owns(channel: string): boolean {
        return this.owned.has(channel);
    }

    ownedChannels(): string[] {
        return [...this.owned];
    }

    queueFor(channel: string): string {
        return laneQueue(channelRoute(this.options.base, channel).queue, this.options.lane);
    }

    async start(): Promise<void> {
        this.owned = new Set(await this.options.loadChannels());

        for (const channel of this.owned) {
            await this.subscribeChannel(channel);
        }
        console.info(
            `[OutboundSubscriptions] ${this.options.base.queue}: owning ` +
                `[${this.ownedChannels().join(', ')}]`,
        );
    }

    async reconcile(): Promise<void> {
        const next = new Set(await this.options.loadChannels());
        const added = [...next].filter((c) => !this.owned.has(c));
        const removed = [...this.owned].filter((c) => !next.has(c));

        // 先换认领集合：removed 的 channel 从这一刻起不再归本进程。这一步必须先于
        // 下面的 drain，而且它不是 diff 的来源 —— 下面两个循环读的是订阅账（真的订上
        // / 真的排空才动），所以任何一步抛错下次都会重做。
        this.owned = next;
        if (added.length > 0 || removed.length > 0) {
            console.info(
                `[OutboundSubscriptions] ${this.options.base.queue}: owned channels now ` +
                    `[${this.ownedChannels().join(', ')}] (added=[${added.join(', ')}], ` +
                    `handed off=[${removed.join(', ')}])`,
            );
        }

        const wanted = new Set([...next].map((c) => this.queueFor(c)));
        for (const channel of next) {
            await this.subscribeChannel(channel);
        }
        for (const queue of this.heldQueues()) {
            if (!wanted.has(queue)) await this.handOff(queue);
        }
    }

    /** 还没确认交还的队列（含两种 `mayBe*`）。 */
    private heldQueues(): string[] {
        return [...this.state].filter(([, state]) => state !== 'released').map(([queue]) => queue);
    }

    /**
     * 订上这条 channel 的队列。已经订着就什么都不做 —— reconcile 每一轮都会调它。
     *
     * mayBeConsuming 是唯一要先 drain 一次的状态：那时端口可能已经在重连里把它订
     * 回来并写上了新 consumerTag，直接再 consume 一次就是 broker 上两个消费者，而
     * 旧那个的 tag 已经被覆盖、再也 cancel 不掉。mayBeReleased 则可以直接订 ——
     * basic.cancel 已经发过，端口复用那一项重新注册，落地正好一个；再 drain 一次
     * 只会继续等那个卡住的在途 handler，而这时候要的是恢复消费。
     */
    private async subscribeChannel(channel: string): Promise<void> {
        const queue = this.queueFor(channel);
        const state = this.state.get(queue) ?? 'released';
        if (state === 'consuming') return;
        if (state === 'mayBeConsuming') await this.handOff(queue);

        const route = channelRoute(this.options.base, channel);
        // 记账先于副作用：consume 抛错时订阅项已经在重连恢复列表里了。
        this.state.set(queue, 'mayBeConsuming');
        await this.options.port.declareRoute(route);
        await this.options.port.consume(
            queue,
            // channel 队列只认自己那一个：队列绑定和 payload 打架时以队列为准，
            // 生产者分流错了要立刻暴露而不是被顺手处理掉。
            this.options.handlerFor((c) => c === channel),
        );
        this.state.set(queue, 'consuming');
    }

    private async handOff(queue: string): Promise<void> {
        // 同理：drainConsumer 先发 basic.cancel 再等在途归零，超时抛错时 broker 侧
        // 已经取消了。
        this.state.set(queue, 'mayBeReleased');
        await this.options.port.drainConsumer(queue);
        this.state.set(queue, 'released');
    }
}
