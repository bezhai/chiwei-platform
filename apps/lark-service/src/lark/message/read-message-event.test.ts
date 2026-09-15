import { describe, expect, it } from 'bun:test';

import type { LarkBotLookup } from './mentions';
import { readLarkMessageEvent } from './read-message-event';
import type { LarkMessageEvent } from './wire';

const noBots: LarkBotLookup = { byAppId: () => null, byUnionId: () => null };

describe('sender classification', () => {
    it.each(['persona', 'utility'] as const)('classifies a configured %s by the sender, not the receiving app', (botRole) => {
        const bot = { botName: 'ayana', botRole, displayName: '绫奈', commonUserId: 'cu_ayana' };
        const bots: LarkBotLookup = {
            byUnionId: (id) => id === 'on_ayana' ? bot : null,
            byAppId: () => { throw new Error('the event app_id is the receiver'); },
        };
        const payload = event('text', '{"text":"hello"}');
        payload.sender = { sender_type: 'bot', sender_id: { union_id: 'on_ayana', open_id: 'ou_ayana' } };
        expect(readLarkMessageEvent(payload, bots)!.sender).toEqual({
            kind: botRole === 'persona' ? 'person' : 'bot', bot,
        });
    });

    it('keeps unconfigured bots distinct from human users', () => {
        const payload = event('text', '{"text":"hello"}');
        expect(readLarkMessageEvent(payload, noBots)!.sender).toEqual({ kind: 'person', bot: null });
        payload.sender.sender_type = 'bot';
        expect(readLarkMessageEvent(payload, noBots)!.sender).toEqual({ kind: 'bot', bot: null });
    });
});

function event(messageType: string, content: string, extra: Record<string, unknown> = {}) {
    return {
        app_id: 'cli_app',
        sender: { sender_type: 'user', sender_id: { union_id: 'on_u', open_id: 'ou_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: messageType,
            content,
            ...extra,
        },
    } as LarkMessageEvent;
}

describe('readLarkMessageEvent', () => {
    it('skips an event that is not a message', () => {
        expect(readLarkMessageEvent({ sender: { sender_type: 'user' } } as LarkMessageEvent, noBots))
            .toBeNull();
    });

    it('hands back the Lark message, both projections and the resolved mentions', () => {
        const mentions = [{ key: '@_user_1', id: { union_id: 'on_a' }, name: '张三' }];
        const reading = readLarkMessageEvent(
            event('text', JSON.stringify({ text: 'hi @_user_1' }), { mentions }),
            noBots,
        )!;

        expect(reading.message.messageId).toBe('om_1');
        expect(reading.mentions.all.map((m) => m.displayName)).toEqual(['张三']);
        expect(reading.content).toEqual([
            { type: 'text', value: 'hi ' },
            {
                type: 'mention',
                value: '张三',
                meta: { channel_user_id: 'on_a', bot_common_user_id: undefined },
            },
        ]);
        expect(reading.inbound.content).toEqual([{ kind: 'text', text: 'hi @张三' }]);
    });

    // 两种形状必须是**同一次解析**的两个投影，不是两条各走一遍的解析路径。可观测
    // 的判据：任何一种消息类型下，两边看到的媒体引用完全一致 —— 一边多一个键或
    // 少一个键，就说明有人在自己那侧又解释了一遍 content。
    it('shows the same media references in both shapes', () => {
        const post = {
            content: [
                [
                    { tag: 'text', text: 'a' },
                    { tag: 'img', image_key: 'img_1' },
                ],
                [{ tag: 'img', image_key: 'img_2' }],
            ],
        };
        const reading = readLarkMessageEvent(event('post', JSON.stringify(post)), noBots)!;

        const larkImages = reading.content
            .filter((part) => part.type === 'image')
            .map((part) => part.value);
        const contractImages = reading.inbound.content
            .filter((item) => item.kind === 'image')
            .map((item) => item.key);

        expect(larkImages).toEqual(['img_1', 'img_2']);
        expect(contractImages).toEqual(larkImages);
        expect(reading.content).toHaveLength(reading.inbound.content.length);
    });

    it('fails loudly when a mentioned bot of ours has no identity yet', () => {
        const bots: LarkBotLookup = {
            byAppId: () => ({ botName: 'chiwei', botRole: 'persona', displayName: '赤尾' }),
            byUnionId: () => null,
        };
        expect(() =>
            readLarkMessageEvent(
                event('text', '{"text":"@_user_1"}', {
                    mentions: [
                        {
                            key: '@_user_1',
                            id: { union_id: 'on_bot' },
                            name: 'raw',
                            mentioned_type: 'bot',
                            bot_info: { app_id: 'cli_a' },
                        },
                    ],
                }),
                bots,
            ),
        ).toThrow(/common_user_id/);
    });
});

it('rejects a configured sender without a canonical identity', () => {
    const payload = event('text', '{"text":"hello"}');
    const bots: LarkBotLookup = {
        byAppId: () => null,
        byUnionId: () => ({ botName: 'broken', botRole: 'persona', displayName: null }),
    };
    expect(() => readLarkMessageEvent(payload, bots)).toThrow(/common_user_id/);
});
