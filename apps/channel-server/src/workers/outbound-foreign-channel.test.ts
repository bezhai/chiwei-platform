// 出站消费者的 fail-closed：不属于自己的 channel 绝不处理。
//
// 两个 handler 放一个文件，因为它们守的是同一条不变量，而这条不变量是共库方案的
// 承重墙：common_agent_response 没有 channel 列，DB 层拦不住越界写入，唯一的隔离
// 手段就是「rk 分对了 + 消费侧不越界」。rk 配错是配置问题，fail-closed 让它立刻
// 暴露而不是静默写脏。
//
// 「拒绝」= nack(requeue=false)：prod 队列挂着 DLX，消息进 dead_letters 可查可重放。
// 绝不 requeue —— 两个服务互相推诿同一条消息会压成活锁（决策八）。
// 「告警」= 带稳定 event 名的 error 日志，可以用 make logs KEYWORD= 直接捞。

import { describe, it, expect } from 'bun:test';
import type { ConsumeMessage } from 'amqplib';

import { handleChatResponse, type ChatResponseHandlerDeps } from './chat-response-handler';
import { handleRecall, type RecallHandlerDeps } from './recall-worker';

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
        const { deps, acked, nacked } = makeDeps((c) => c === 'qq');
        const capture = captureErrors();

        try {
            await handleChatResponse(
                deps,
                makeMsg({
                    channel: 'lark',
                    session_id: 's1',
                    message_id: 'm1',
                    chat_id: 'c1',
                    is_p2p: true,
                    content: '你好',
                    status: 'success',
                }),
            );
        } finally {
            capture.restore();
        }

        expect(nacked).toHaveLength(1);
        expect(nacked[0]!.requeue).toBe(false);
        expect(acked).toHaveLength(0);
        expect(capture.lines.join('\n')).toContain('chat_response_foreign_channel');
        expect(capture.lines.join('\n')).toContain('lark');
    });

    it('自己的 channel：照常往下走（这里靠 repo 被调到证明没被拦）', async () => {
        const { deps } = makeDeps((c) => c === 'lark');
        let queried = false;
        (deps as { repo: unknown }).repo = {
            findOneBy: async () => {
                queried = true;
                return null;
            },
        } as unknown as ChatResponseHandlerDeps['repo'];

        await handleChatResponse(
            deps,
            makeMsg({
                channel: 'lark',
                session_id: 's1',
                message_id: 'm1',
                chat_id: 'c1',
                is_p2p: true,
                content: '你好',
                status: 'success',
            }),
        );

        expect(queried).toBe(true);
    });
});

describe('recall handler fail-closed', () => {
    function makeDeps(owns: (channel: string) => boolean) {
        const acked: ConsumeMessage[] = [];
        const nacked: Array<{ msg: ConsumeMessage; requeue?: boolean }> = [];
        const deps: RecallHandlerDeps = {
            repo: {
                findOneBy: async () => forbidden('repo.findOneBy'),
                update: async () => forbidden('repo.update'),
            } as unknown as RecallHandlerDeps['repo'],
            ownsChannel: owns,
            getCapabilities: () => forbidden('getCapabilities'),
            republish: async () => forbidden('republish'),
            ack: (msg) => acked.push(msg),
            nack: (msg, requeue) => nacked.push({ msg, requeue }),
        };
        return { deps, acked, nacked };
    }

    // safety_status / safety_result 是写入矩阵里的字段级重叠：recall worker 和
    // agent-service 双向写同一列，而表上没有 channel 列。越界的 recall 一旦被
    // 处理，写脏的是另一个服务的台账。
    it('别人的 channel：不查台账、不撤回、nack 到 DLQ', async () => {
        const { deps, acked, nacked } = makeDeps((c) => c === 'qq');
        const capture = captureErrors();

        try {
            await handleRecall(
                deps,
                makeMsg({ channel: 'lark', session_id: 's1', reason: 'unsafe' }),
            );
        } finally {
            capture.restore();
        }

        expect(nacked).toHaveLength(1);
        expect(nacked[0]!.requeue).toBe(false);
        expect(acked).toHaveLength(0);
        expect(capture.lines.join('\n')).toContain('recall_foreign_channel');
    });

    it('自己的 channel：照常往下走', async () => {
        const { deps } = makeDeps((c) => c === 'lark');
        let queried = false;
        (deps as { repo: unknown }).repo = {
            findOneBy: async () => {
                queried = true;
                return { safety_status: 'recalled', replies: [] };
            },
        } as unknown as RecallHandlerDeps['repo'];

        await handleRecall(deps, makeMsg({ channel: 'lark', session_id: 's1', reason: 'unsafe' }));

        expect(queried).toBe(true);
    });
});
