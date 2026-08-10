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

class FakePort {
    readonly declared: Route[] = [];
    readonly subscribed: Subscribed[] = [];
    readonly drained: string[] = [];

    async declareRoute(route: Route): Promise<void> {
        this.declared.push(route);
    }
    async consume(queue: string, handler: MessageHandler): Promise<void> {
        this.subscribed.push({ queue, handler });
    }
    async drainConsumer(queue: string): Promise<void> {
        this.drained.push(queue);
        const idx = this.subscribed.findIndex((s) => s.queue === queue);
        if (idx >= 0) this.subscribed.splice(idx, 1);
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
        port.drainConsumer = async () => {
            throw new Error('broker gone');
        };
        let owned = ['lark', 'qq'];
        const { subs } = makeSubs(port, async () => owned);
        await subs.start();

        owned = ['qq'];
        await expect(subs.reconcile()).rejects.toThrow('broker gone');
        expect(subs.owns('lark')).toBe(false);
    });
});
