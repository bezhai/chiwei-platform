// 出站消费的双订阅与运行期收窄。
//
// 换队列的协议是「消费侧先双订阅 → 切生产者 → 旧队列排空 → drain 屏障移交」。这里
// 负责第一步和最后一步：worker 同时守着旧的 chat_response 和新的 chat_response_{channel}，
// 于是 agent-service 什么时候切 rk 都不在关键路径上；移交某个 channel 时先把它从
// 拥有集合里摘掉（旧队列上再收到它就 fail-closed），再对它自己的队列走 drain 屏障。
//
// 移交必须是 drain 而不是「停旧起新」：旧 worker 已经调完平台 API、还没 ACK 的那一
// 瞬间被杀，消息 requeue 换个消费者再发一次，真人看到两条。

import { describe, it, expect } from 'bun:test';
import type { ConsumeMessage } from 'amqplib';
import { CHAT_RESPONSE, type MessageHandler, type Route } from '@inner/shared/mq';

import { OutboundSubscriptions } from './outbound-subscriptions';

interface Subscribed {
    queue: string;
    handler: MessageHandler;
}

/**
 * 替身按**真实端口的副作用顺序**来：两个入口都是「先产生副作用、再可能失败」。
 *
 * `RabbitMQClient.consume` 先把订阅项 push 进重连恢复列表再去 broker 注册，注册抛错
 * 时那一项已经在列表里且可恢复（断线重连会把它订回来）；`drainConsumer` 先摘掉重连
 * 资格、先发 basic.cancel，再等在途归零，超时抛错时 broker 侧已经取消了。
 *
 * 「什么都没做就抛」的替身会把这两件事说成"失败=什么都没发生"，于是错误地证明
 * 「下一次一定自愈」。
 */
class FakePort {
    readonly declared: Route[] = [];
    /** broker 侧真的在投递的消费者。 */
    readonly subscribed: Subscribed[] = [];
    /** 端口的重连恢复列表：断线重连会把这里面的订阅项重新注册。 */
    private readonly recovering = new Map<string, MessageHandler>();
    readonly drained: string[] = [];
    /** 订阅这条队列时抛错 —— 恢复列表已经登记，broker 侧没注册上。 */
    failConsumeOn?: string;
    /** drain 这条队列时抛错（在途没归零）—— basic.cancel 已经发出去了。 */
    failDrainOn?: string;

    async declareRoute(route: Route): Promise<void> {
        this.declared.push(route);
    }
    async consume(queue: string, handler: MessageHandler): Promise<void> {
        // 先进恢复列表、再去 broker 注册：注册抛错时那一项已经躺在列表里且可恢复。
        this.recovering.set(queue, handler);
        if (this.failConsumeOn === queue) throw new Error(`broker refused ${queue}`);
        this.subscribed.push({ queue, handler });
    }
    async drainConsumer(queue: string): Promise<void> {
        // 先摘重连资格、先发 basic.cancel，再等在途归零：超时抛错时前两件已经做完。
        this.recovering.delete(queue);
        this.drained.push(queue);
        const idx = this.subscribed.findIndex((s) => s.queue === queue);
        if (idx >= 0) this.subscribed.splice(idx, 1);
        if (this.failDrainOn === queue) {
            throw new Error(`[RabbitMQ] drain timed out on ${queue}`);
        }
    }

    /** 断线重连：恢复列表里的订阅项被重新注册（真实端口 5 秒后就会做这件事）。 */
    reconnect(): void {
        for (const [queue, handler] of this.recovering) {
            if (!this.subscribed.some((s) => s.queue === queue)) {
                this.subscribed.push({ queue, handler });
            }
        }
    }

    queues(): string[] {
        return this.subscribed.map((s) => s.queue);
    }
    handlerOn(queue: string): MessageHandler {
        const found = this.subscribed.find((s) => s.queue === queue);
        if (!found) throw new Error(`no consumer on ${queue}`);
        return found.handler;
    }
}

function makeMsg(channel: string): ConsumeMessage {
    return {
        content: Buffer.from(JSON.stringify({ channel })),
        fields: {} as ConsumeMessage['fields'],
        properties: {} as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

/** 把 handler 收到的 channel 与它当时的判定结果记下来。 */
function makeSubs(
    port: FakePort,
    channels: () => Promise<string[]>,
    lane?: string,
): { subs: OutboundSubscriptions; handled: Array<{ channel: string; accepted: boolean }> } {
    const handled: Array<{ channel: string; accepted: boolean }> = [];
    const subs = new OutboundSubscriptions({
        base: CHAT_RESPONSE,
        lane,
        port,
        loadChannels: channels,
        handlerFor: (accepts) => async (msg: ConsumeMessage) => {
            const channel = JSON.parse(msg.content.toString()).channel as string;
            handled.push({ channel, accepted: accepts(channel) });
        },
    });
    return { subs, handled };
}

describe('start — 双订阅', () => {
    it('prod：同时订阅旧队列和每个拥有渠道的新队列', async () => {
        const port = new FakePort();
        const { subs } = makeSubs(port, async () => ['lark', 'qq']);

        await subs.start();

        expect(port.queues()).toEqual(['chat_response', 'chat_response_lark', 'chat_response_qq']);
        expect(port.declared.map((r) => r.queue)).toEqual([
            'chat_response_lark',
            'chat_response_qq',
        ]);
    });

    it('泳道：泳道后缀加在 channel 之后', async () => {
        const port = new FakePort();
        const { subs } = makeSubs(port, async () => ['lark', 'qq'], 'ppe-x');

        await subs.start();

        expect(port.queues()).toEqual([
            'chat_response_ppe-x',
            'chat_response_lark_ppe-x',
            'chat_response_qq_ppe-x',
        ]);
    });

    it('新旧两套队列各投一条都能被处理', async () => {
        const port = new FakePort();
        const { subs, handled } = makeSubs(port, async () => ['lark', 'qq']);
        await subs.start();

        await port.handlerOn('chat_response')(makeMsg('lark'));
        await port.handlerOn('chat_response_lark')(makeMsg('lark'));

        expect(handled).toEqual([
            { channel: 'lark', accepted: true },
            { channel: 'lark', accepted: true },
        ]);
    });

    it('每条 channel 队列只认自己的 channel', async () => {
        // 队列绑定和 payload 打架时以队列为准：生产者分流错了要立刻暴露。
        const port = new FakePort();
        const { subs, handled } = makeSubs(port, async () => ['lark', 'qq']);
        await subs.start();

        await port.handlerOn('chat_response_lark')(makeMsg('qq'));

        expect(handled).toEqual([{ channel: 'qq', accepted: false }]);
    });

    it('旧队列按当前拥有集合判定（它上面什么 channel 都可能来）', async () => {
        const port = new FakePort();
        const { subs, handled } = makeSubs(port, async () => ['lark', 'qq']);
        await subs.start();

        await port.handlerOn('chat_response')(makeMsg('wechat'));

        expect(handled).toEqual([{ channel: 'wechat', accepted: false }]);
    });
});

describe('reconcile — 运行期收窄', () => {
    it('移交某个 channel：先不再拥有它，再 drain 它自己的队列', async () => {
        const port = new FakePort();
        let owned = ['lark', 'qq'];
        const { subs, handled } = makeSubs(port, async () => owned);
        await subs.start();
        expect(subs.owns('lark')).toBe(true);

        owned = ['qq'];
        await subs.reconcile();

        expect(subs.owns('lark')).toBe(false);
        expect(port.drained).toEqual(['chat_response_lark']);
        expect(port.queues()).toEqual(['chat_response', 'chat_response_qq']);

        // 旧队列上再来一条 lark 就不再被认领 —— 这正是「收窄早了」的告警信号。
        await port.handlerOn('chat_response')(makeMsg('lark'));
        expect(handled).toEqual([{ channel: 'lark', accepted: false }]);
    });

    it('回滚：把 channel 加回来会重新声明并订阅', async () => {
        const port = new FakePort();
        let owned = ['qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        owned = ['qq', 'lark'];
        await subs.reconcile();

        expect(subs.owns('lark')).toBe(true);
        expect(port.queues()).toEqual(['chat_response', 'chat_response_qq', 'chat_response_lark']);
        expect(port.declared.map((r) => r.queue)).toEqual([
            'chat_response_qq',
            'chat_response_lark',
        ]);
    });

    it('没变化时不重复订阅、不 drain', async () => {
        const port = new FakePort();
        const { subs } = makeSubs(port, async () => ['lark', 'qq']);
        await subs.start();
        const before = port.queues().length;

        await subs.reconcile();

        expect(port.queues().length).toBe(before);
        expect(port.drained).toEqual([]);
    });

    it('drain 失败时拥有集合不回退（消息已经不该由我处理了），错误抛给调用方', async () => {
        const port = new FakePort();
        port.failDrainOn = 'chat_response_lark';
        let owned = ['lark', 'qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        owned = ['qq'];
        await expect(subs.reconcile()).rejects.toThrow('drain timed out');
        expect(subs.owns('lark')).toBe(false);
    });
});

// ---------------------------------------------------------------------------
// 失败之后还能重试
// ---------------------------------------------------------------------------

describe('reconcile — 失败的那一步下次还会做', () => {
    it('订阅抛错：下一次 reconcile 补上它，而不是把失败永久固化', async () => {
        // 认领集合是 diff 的来源，也是旧队列 fail-closed 的依据。它要是在真的订上
        // 之前就提交，失败之后算出来的 diff 就是空的 —— 这条 channel 的队列从此
        // 没有消费者，而它已经被认领，没有任何一侧会再管它。
        const port = new FakePort();
        let owned = ['qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        port.failConsumeOn = 'chat_response_lark';
        owned = ['qq', 'lark'];
        await expect(subs.reconcile()).rejects.toThrow('broker refused chat_response_lark');

        port.failConsumeOn = undefined;
        await subs.reconcile();

        expect(port.queues()).toContain('chat_response_lark');
    });

    it('订阅抛错、端口重连把它订回来：重订之前先摘干净，broker 上只留一个消费者', async () => {
        // consume 抛错时订阅项已经在端口的重连恢复列表里，重连会把它订回来并写上新
        // 的 consumerTag。直接再 consume 一次 = broker 上两个消费者，旧那个的 tag
        // 已经被覆盖、再也 cancel 不掉 —— 它会活过下一次移交。
        const port = new FakePort();
        let owned = ['qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        port.failConsumeOn = 'chat_response_lark';
        owned = ['qq', 'lark'];
        await expect(subs.reconcile()).rejects.toThrow('broker refused');
        expect(port.queues()).not.toContain('chat_response_lark');

        port.reconnect();
        port.failConsumeOn = undefined;
        await subs.reconcile();

        expect(port.drained).toEqual(['chat_response_lark']);
        expect(port.queues().filter((q) => q === 'chat_response_lark')).toHaveLength(1);
    });

    it('订阅抛错之后那条 channel 又被移交：照样 drain 掉它', async () => {
        // 抛错不代表没订上。记成"没订上"的话，移交时看不出 diff，重连恢复出来的
        // 那个消费者永远不会被 cancel。
        const port = new FakePort();
        let owned = ['qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        port.failConsumeOn = 'chat_response_lark';
        owned = ['qq', 'lark'];
        await expect(subs.reconcile()).rejects.toThrow('broker refused');

        port.failConsumeOn = undefined;
        owned = ['qq'];
        await subs.reconcile();

        expect(port.drained).toEqual(['chat_response_lark']);
    });

    it('drain 抛错：下一次 reconcile 再排一次，不是就此认定交出去了', async () => {
        // drain 超时抛错时 basic.cancel 已经发了，但在途 handler 还没跑完 —— 交接
        // 屏障没有真正完成。下一次要继续等它归零，而不是当作已经交割。
        const port = new FakePort();
        port.failDrainOn = 'chat_response_lark';
        let owned = ['lark', 'qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        owned = ['qq'];
        await expect(subs.reconcile()).rejects.toThrow('drain timed out');

        port.failDrainOn = undefined;
        await subs.reconcile();

        expect(port.drained).toEqual(['chat_response_lark', 'chat_response_lark']);
        expect(subs.owns('lark')).toBe(false);
    });

    it('drain 抛错之后那条 channel 又被加回来：重新订上', async () => {
        // basic.cancel 已经发出去了，broker 侧不再投递。记成"还订着"的话，回滚路径
        // 上看不出 diff —— 这条队列从此没有任何消费者。
        const port = new FakePort();
        port.failDrainOn = 'chat_response_lark';
        let owned = ['lark', 'qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        owned = ['qq'];
        await expect(subs.reconcile()).rejects.toThrow('drain timed out');

        port.failDrainOn = undefined;
        owned = ['qq', 'lark'];
        await subs.reconcile();

        expect(port.queues()).toContain('chat_response_lark');
        expect(subs.owns('lark')).toBe(true);
    });
});
