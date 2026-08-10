import { describe, expect, it } from 'bun:test';
import { context } from '@inner/shared/middleware';

import { larkSpeakAs } from './bot-context';

describe('larkSpeakAs', () => {
    // 飞书客户端池按 context.getBotName() 选客户端（见 sdk-lark-api.ts）。这一跳接错
    // 的症状是「无论谁该说话，都从同一个 bot 发出去」——用户看见另一个人设开口。
    it('puts the bot in the context the Lark client pool reads', async () => {
        let seen = '';
        await larkSpeakAs({ botName: 'chiwei' }, async () => {
            seen = context.getBotName();
        });
        expect(seen).toBe('chiwei');
    });

    it('puts the lane in the context too', async () => {
        let seen = '';
        await larkSpeakAs({ botName: 'chiwei', lane: 'ppe-x' }, async () => {
            seen = context.getLane();
        });
        expect(seen).toBe('ppe-x');
    });

    it('mints a trace id so one segment is one traceable chain', async () => {
        let seen = '';
        await larkSpeakAs({ botName: 'chiwei' }, async () => {
            seen = context.getTraceId();
        });
        expect(seen).not.toBe('');
    });

    it('每段各自一条 trace，不互相串', async () => {
        const traces: string[] = [];
        const collect = async () => void traces.push(context.getTraceId());
        await larkSpeakAs({ botName: 'chiwei' }, collect);
        await larkSpeakAs({ botName: 'chiwei' }, collect);
        expect(traces[0]).not.toBe(traces[1]);
    });

    // 上下文只在这一段里有效：出站是并发消费，泄漏出去就会让下一条消息读到上一条
    // 的 bot。
    it('leaves nothing behind afterwards', async () => {
        await larkSpeakAs({ botName: 'chiwei' }, async () => {});
        expect(context.getBotName()).toBe('');
    });

    it('propagates what the callback throws', async () => {
        await expect(
            larkSpeakAs({ botName: 'chiwei' }, async () => {
                throw new Error('boom');
            }),
        ).rejects.toThrow('boom');
    });
});
