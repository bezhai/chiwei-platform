// 出站消费的双订阅与运行期收窄。
//
// 换队列的协议是「消费侧先双订阅 → 切生产者 → 旧队列排空 → drain 屏障移交」，四步
// 都不能省：生产者和消费者在不同的 Deployment 里，不可能原子发布。先切生产者，旧
// 消费者守着空队列；先切消费者，新队列没有生产者。消费侧同时守着新旧两套队列，
// agent-service 什么时候切 rk 就不在关键路径上了。
//
// 移交某个 channel 时顺序是：先把它从拥有集合里摘掉（旧队列上再收到它就走
// fail-closed，是「收窄早了」的告警信号），再对它自己的队列走 drain 屏障
// （basic.cancel → 等在途归零）。反过来做会留下一个「已经不订阅了、但还认领」的
// 窗口。

import { channelRoute, laneQueue, type MessageHandler, type Route } from '@inner/shared/mq';

export interface OutboundSubscriptionPort {
    declareRoute(route: Route): Promise<void>;
    consume(queue: string, handler: MessageHandler): Promise<void>;
    drainConsumer(queue: string): Promise<void>;
}

export interface OutboundSubscriptionsOptions {
    /** 渠道无关的基础路由（CHAT_RESPONSE / RECALL）。 */
    base: Route;
    lane?: string;
    port: OutboundSubscriptionPort;
    /**
     * 造一个 handler，它只处理 accepts 放行的 channel。
     * 旧队列拿到的是「当前拥有集合」，channel 队列拿到的是「只认自己那个」。
     */
    handlerFor: (accepts: (channel: string) => boolean) => MessageHandler;
    loadChannels: () => Promise<string[]>;
}

export class OutboundSubscriptions {
    private readonly options: OutboundSubscriptionsOptions;
    private owned = new Set<string>();

    constructor(options: OutboundSubscriptionsOptions) {
        this.options = options;
    }

    owns(channel: string): boolean {
        return this.owned.has(channel);
    }

    ownedChannels(): string[] {
        return [...this.owned];
    }

    /** 旧的、不带 channel 维度的队列。cutover 窗口内它上面什么 channel 都可能来。 */
    legacyQueue(): string {
        return laneQueue(this.options.base.queue, this.options.lane);
    }

    queueFor(channel: string): string {
        return laneQueue(channelRoute(this.options.base, channel).queue, this.options.lane);
    }

    async start(): Promise<void> {
        this.owned = new Set(await this.options.loadChannels());

        // 旧队列按「当前拥有集合」判定，所以它读的是活的 this.owned，不是启动时的快照。
        await this.options.port.consume(
            this.legacyQueue(),
            this.options.handlerFor((channel) => this.owns(channel)),
        );

        for (const channel of this.owned) {
            await this.subscribeChannel(channel);
        }
        console.info(
            `[OutboundSubscriptions] ${this.options.base.queue}: owning ` +
                `[${this.ownedChannels().join(', ')}], legacy queue ${this.legacyQueue()}`,
        );
    }

    async reconcile(): Promise<void> {
        const next = new Set(await this.options.loadChannels());
        const added = [...next].filter((c) => !this.owned.has(c));
        const removed = [...this.owned].filter((c) => !next.has(c));
        if (added.length === 0 && removed.length === 0) return;

        // 先换集合：从这一刻起旧队列上的 removed channel 不再被认领。
        this.owned = next;
        console.info(
            `[OutboundSubscriptions] ${this.options.base.queue}: owned channels now ` +
                `[${this.ownedChannels().join(', ')}] (added=[${added.join(', ')}], ` +
                `handed off=[${removed.join(', ')}])`,
        );

        for (const channel of added) {
            await this.subscribeChannel(channel);
        }
        for (const channel of removed) {
            await this.options.port.drainConsumer(this.queueFor(channel));
        }
    }

    private async subscribeChannel(channel: string): Promise<void> {
        const route = channelRoute(this.options.base, channel);
        await this.options.port.declareRoute(route);
        await this.options.port.consume(
            laneQueue(route.queue, this.options.lane),
            // channel 队列只认自己那一个：队列绑定和 payload 打架时以队列为准，
            // 生产者分流错了要立刻暴露而不是被顺手处理掉。
            this.options.handlerFor((c) => c === channel),
        );
    }
}
