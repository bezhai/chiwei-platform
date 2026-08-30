// 出站消费者的 fail-closed：不属于自己的 channel 绝不处理。
//
// 这条不变量是共库方案的承重墙：common_agent_response 没有 channel 列，DB 层拦不住
// 越界写入，唯一的隔离手段就是「rk 分对了 + 消费侧不越界」。rk 配错是配置问题，
// fail-closed 让它立刻暴露而不是静默写脏另一个服务的台账。
//
// 「拒绝」= nack(requeue=false)：prod 队列挂着 DLX，消息进 dead_letters 可查可重放。
// 绝不 requeue —— 消息只会原样退回这条队列，下一轮还是本进程拿到，在这里转圈。
// 「告警」= 带稳定 event 名的 error 日志，可以用 make logs KEYWORD= 直接捞。
//
// 外来渠道的 fixture 用 `telegram`：它从来不是本服务的渠道，所以「这条不归我管」这个
// 语义不依赖本服务当下有几个渠道 —— 拿一个本服务真的消费过的渠道当 fixture，等它被
// 拆出去那天这个用例就悄悄变成了「自己的渠道」的重复覆盖。

import { describe, it, expect } from 'bun:test';
import type { ConsumeMessage } from 'amqplib';

import { handleChatResponse, type ChatResponseHandlerDeps } from './chat-response-handler';

const FOREIGN_CHANNEL = 'telegram';
const OWN_CHANNEL = 'qq';

function makeMsg(payload: Record<string, unknown>): ConsumeMessage {
    return {
        content: Buffer.from(JSON.stringify(payload)),
        fields: {} as ConsumeMessage['fields'],
        properties: { headers: {} } as unknown as ConsumeMessage['properties'],
    } as ConsumeMessage;
}

/** 拒绝路径上一个都不该被碰到的依赖。 */
function forbidden(name: string): never {
    throw new Error(`${name} must not be reached for a foreign-channel message`);
}

function captureErrors(): { lines: string[]; restore: () => void } {
    const lines: string[] = [];
    const original = console.error;
    console.error = (...args: unknown[]): void => {
        lines.push(args.map((a) => (typeof a === 'string' ? a : String(a))).join(' '));
    };
    return { lines, restore: () => (console.error = original) };
}

function chatResponsePayload(channel: string): Record<string, unknown> {
    return {
        channel,
        session_id: 's1',
        message_id: 'm1',
        chat_id: 'c1',
        is_p2p: true,
        content: '你好',
        status: 'success',
    };
}

describe('chat-response handler fail-closed', () => {
    function makeDeps(owns: (channel: string) => boolean) {
        const acked: ConsumeMessage[] = [];
        const nacked: Array<{ msg: ConsumeMessage; requeue?: boolean }> = [];
        const deps: ChatResponseHandlerDeps = {
            repo: {
                findOneBy: async () => forbidden('repo.findOneBy'),
            } as unknown as ChatResponseHandlerDeps['repo'],
            ownsChannel: owns,
            getCapabilities: () => forbidden('getCapabilities'),
            ack: (msg) => acked.push(msg),
            nack: (msg, requeue) => nacked.push({ msg, requeue }),
            observeDuration: () => {},
            observeQueueDelay: () => {},
        };
        return { deps, acked, nacked };
    }

    it('别人的 channel：不查库、不取插件、nack 到 DLQ', async () => {
        const { deps, acked, nacked } = makeDeps((c) => c === OWN_CHANNEL);
        const capture = captureErrors();

        try {
            await handleChatResponse(deps, makeMsg(chatResponsePayload(FOREIGN_CHANNEL)));
        } finally {
            capture.restore();
        }

        expect(nacked).toHaveLength(1);
        expect(nacked[0]!.requeue).toBe(false);
        expect(acked).toHaveLength(0);
        expect(capture.lines.join('\n')).toContain('chat_response_foreign_channel');
        expect(capture.lines.join('\n')).toContain(FOREIGN_CHANNEL);
    });

    // payload 不带 channel 曾经按飞书处理。本服务已经没有飞书的出站能力了，再按飞书
    // 算只会在 getCapabilities 那一步炸得莫名其妙 —— 而且那时消息已经被认领。缺 channel
    // 是生产者的问题，要在认领之前就退回去，跟收到别人的渠道一个处置。
    it('payload 没有 channel：不猜渠道，nack 到 DLQ', async () => {
        const { deps, acked, nacked } = makeDeps((c) => c === OWN_CHANNEL);
        const capture = captureErrors();
        const { channel: _channel, ...noChannel } = chatResponsePayload(OWN_CHANNEL);

        try {
            await handleChatResponse(deps, makeMsg(noChannel));
        } finally {
            capture.restore();
        }

        expect(nacked).toHaveLength(1);
        expect(nacked[0]!.requeue).toBe(false);
        expect(acked).toHaveLength(0);
        expect(capture.lines.join('\n')).toContain('chat_response_channel_missing');
    });

    it('自己的 channel：照常往下走（这里靠 repo 被调到证明没被拦）', async () => {
        const { deps } = makeDeps((c) => c === OWN_CHANNEL);
        let queried = false;
        (deps as { repo: unknown }).repo = {
            findOneBy: async () => {
                queried = true;
                return null;
            },
        } as unknown as ChatResponseHandlerDeps['repo'];

        await handleChatResponse(deps, makeMsg(chatResponsePayload(OWN_CHANNEL)));

        expect(queried).toBe(true);
    });
});
