// channel-server 出站消费的订阅面：订哪条队列、handler 认哪个渠道。
//
// 飞书移交给 lark-service 之后本服务的出站只剩 QQ，订阅是一条无条件的直路：声明
// chat_response_qq、订上、handler 只认 qq。没有开关、没有运行期收窄、没有 drain 屏障
// —— 那套是切流期两个服务共守同一条出站队列时的移交脚手架，随切流一起删了。
//
// 外来渠道的 fixture 用 telegram：它从来不是本服务的渠道，所以「这条不归我管」这个
// 语义不依赖本服务当下有几个渠道。

import { describe, it, expect } from 'bun:test';
import type { ConsumeMessage } from 'amqplib';
import type { MessageHandler, Route } from '@inner/shared/mq';

import {
    CHANNEL_SERVER_OUTBOUND_CHANNEL,
    subscribeChatResponse,
} from './chat-response-subscription';

/**
 * 端口替身。**只有 declareRoute 和 consume 两件事** —— 没有 drainConsumer，因为
 * 订阅不再需要能被撤回。这个替身的表面本身就是断言：多出一个方法说明移交脚手架
 * 又长回来了。
 */
class FakePort {
    readonly declared: Route[] = [];
    readonly subscribed: Array<{ queue: string; handler: MessageHandler }> = [];
    /** 带顺序的流水：订一条没声明的队列等于守着空气，先后顺序要钉住。 */
    readonly ops: string[] = [];

    async declareRoute(route: Route): Promise<void> {
        this.declared.push(route);
        this.ops.push(`declare ${route.queue}`);
    }

    async consume(queue: string, handler: MessageHandler): Promise<void> {
        this.subscribed.push({ queue, handler });
        this.ops.push(`consume ${queue}`);
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

/** 订上，并把 handler 收到的 channel 与它当时的判定结果记下来。 */
async function subscribe(
    port: FakePort,
    lane?: string,
): Promise<{ queue: string; handled: Array<{ channel: string; accepted: boolean }> }> {
    const handled: Array<{ channel: string; accepted: boolean }> = [];
    const queue = await subscribeChatResponse({
        port,
        lane,
        handlerFor: (accepts) => async (msg: ConsumeMessage) => {
            const channel = JSON.parse(msg.content.toString()).channel as string;
            handled.push({ channel, accepted: accepts(channel) });
        },
    });
    return { queue, handled };
}

describe('subscribeChatResponse — 无条件订自己那一条', () => {
    it('prod：声明并订上 chat_response_qq，就这一条', async () => {
        const port = new FakePort();

        const { queue } = await subscribe(port);

        expect(queue).toBe('chat_response_qq');
        expect(port.subscribed.map((s) => s.queue)).toEqual(['chat_response_qq']);
        expect(port.declared).toEqual([{ queue: 'chat_response_qq', rk: 'chat.response.qq' }]);
    });

    it('先声明再订阅：订一条没声明的队列等于守着空气', async () => {
        const port = new FakePort();

        await subscribe(port);

        expect(port.ops).toEqual(['declare chat_response_qq', 'consume chat_response_qq']);
    });

    it('泳道后缀加在 channel 之后', async () => {
        const port = new FakePort();

        const { queue } = await subscribe(port, 'ppe-x');

        expect(queue).toBe('chat_response_qq_ppe-x');
        expect(port.subscribed.map((s) => s.queue)).toEqual(['chat_response_qq_ppe-x']);
        // 声明传的是不带泳道后缀的基础路由：真实 MQ 端口的 declareRoute 自己按 LANE
        // 加后缀（client.ts::declareRoute），这里再加一次就是声明了个不存在的队列名。
        expect(port.declared).toEqual([{ queue: 'chat_response_qq', rk: 'chat.response.qq' }]);
    });
});

describe('handler 的渠道判定', () => {
    it('队列上出现别的渠道的消息：不放行', async () => {
        // 队列绑定和 payload 打架时以队列为准：生产者分流错了要立刻暴露。
        // 落到 handler 之后的处置（nack 到 DLQ + 告警）见 outbound-foreign-channel.test.ts。
        const port = new FakePort();
        const { queue, handled } = await subscribe(port);

        await port.handlerOn(queue)(makeMsg('telegram'));

        expect(handled).toEqual([{ channel: 'telegram', accepted: false }]);
    });

    it('自己的渠道照常放行', async () => {
        const port = new FakePort();
        const { queue, handled } = await subscribe(port);

        await port.handlerOn(queue)(makeMsg('qq'));

        expect(handled).toEqual([{ channel: 'qq', accepted: true }]);
    });
});

describe('出站渠道', () => {
    it('飞书移交之后 channel-server 的出站只剩 qq', () => {
        expect(CHANNEL_SERVER_OUTBOUND_CHANNEL).toBe('qq');
    });
});
