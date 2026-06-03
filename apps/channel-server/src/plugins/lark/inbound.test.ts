import { describe, it, expect } from 'bun:test';
import {
    assertValidInboundMessage,
    type InboundMessage,
} from '@core/channels/contracts';
import { larkInbound } from './inbound';
import type { LarkReceiveMessage } from 'types/lark';

const LARK = 'lark';

function p2pTextEvent(): LarkReceiveMessage {
    return {
        app_id: 'cli_app',
        sender: { sender_id: { union_id: 'on_user', open_id: 'ou_user' }, sender_type: 'user' },
        message: {
            message_id: 'om_msg1',
            chat_id: 'oc_chat1',
            chat_type: 'p2p',
            message_type: 'text',
            create_time: '1700000000000',
            content: JSON.stringify({ text: 'hello bot' }),
        },
    };
}

function groupMentionEvent(mentionUnionIds: string[]): LarkReceiveMessage {
    return {
        app_id: 'cli_app',
        sender: { sender_id: { union_id: 'on_sender', open_id: 'ou_sender' }, sender_type: 'user' },
        message: {
            message_id: 'om_msg2',
            chat_id: 'oc_group',
            chat_type: 'group',
            message_type: 'text',
            create_time: '1700000001000',
            root_id: 'om_root',
            parent_id: 'om_parent',
            content: JSON.stringify({ text: '@_user_1 hi' }),
            mentions: mentionUnionIds.map((uid, i) => ({
                key: `@_user_${i + 1}`,
                id: { union_id: uid, open_id: `ou_${uid}` },
                name: uid,
                mentioned_type: 'user',
            })),
        },
    };
}

function mixedMentionTextEvent(): LarkReceiveMessage {
    return {
        app_id: 'cli_app',
        sender: { sender_id: { union_id: 'on_sender', open_id: 'ou_sender' }, sender_type: 'user' },
        message: {
            message_id: 'om_mixed_mentions',
            chat_id: 'oc_group',
            chat_type: 'group',
            message_type: 'text',
            create_time: '1700000002000',
            content: JSON.stringify({ text: '@_user_1 @_user_2 你俩认识@_user_3 么' }),
            mentions: [
                {
                    key: '@_user_2',
                    id: { union_id: 'on_bot2', open_id: 'ou_bot2' },
                    name: '绫奈',
                    mentioned_type: 'bot',
                    bot_info: { app_id: 'cli_bot2' },
                },
                {
                    key: '@_user_1',
                    id: { union_id: 'on_bot1', open_id: 'ou_bot1' },
                    name: '赤尾',
                    mentioned_type: 'bot',
                    bot_info: { app_id: 'cli_bot1' },
                },
                {
                    key: '@_user_3',
                    id: { union_id: 'on_human', open_id: 'ou_human' },
                    name: '原智鸿',
                    mentioned_type: 'user',
                },
            ],
        },
    };
}

function eventOfType(messageType: string, content: unknown): LarkReceiveMessage {
    const ev = p2pTextEvent();
    ev.message.message_type = messageType;
    ev.message.content = JSON.stringify(content);
    return ev;
}

describe('larkInbound.handleHandshake', () => {
    it('echoes challenge for url_verification', () => {
        expect(
            larkInbound.handleHandshake({ type: 'url_verification', challenge: 'abc' }),
        ).toEqual({ challenge: 'abc' });
    });

    it('returns null for non-handshake events', () => {
        expect(larkInbound.handleHandshake({ type: 'event_callback' })).toBeNull();
        expect(larkInbound.handleHandshake(p2pTextEvent())).toBeNull();
    });
});

describe('larkInbound.verify', () => {
    it('is constant-true after webhook ingress verification', () => {
        expect(larkInbound.verify({})).toBe(true);
    });
});

describe('larkInbound.parse', () => {
    it('maps p2p text event to a valid InboundMessage with scope=direct', () => {
        const msg = larkInbound.parse(p2pTextEvent()) as InboundMessage;
        expect(msg).not.toBeNull();
        assertValidInboundMessage(msg);
        expect(msg.channel).toBe(LARK);
        expect(msg.channel_message_id).toBe('om_msg1');
        expect(msg.channel_chat_id).toBe('oc_chat1');
        expect(msg.channel_user_id).toBe('ou_user');
        expect(msg.conversation_scope).toBe('direct');
        expect(msg.thread_ref).toEqual({
            selfChannelMessageId: 'om_msg1',
            inThread: true,
        });
        expect(msg.addressing_hints).toEqual([]);
        expect(msg.content).toEqual([{ kind: 'text', text: 'hello bot' }]);
        expect(msg.received_at).toBe(1700000000000);
    });

    it('maps group event: scope=group, mentions->addressing_hints(union_id), thread_ref root/parent', () => {
        const msg = larkInbound.parse(
            groupMentionEvent(['on_bot', 'on_other']),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.conversation_scope).toBe('group');
        expect(msg.addressing_hints).toEqual([
            { targetId: 'on_bot' },
            { targetId: 'on_other' },
        ]);
        expect(msg.content).toEqual([
            {
                kind: 'mention',
                id: 'on_bot',
                label: 'on_bot',
                meta: { open_id: 'ou_on_bot', union_id: 'on_bot', mentioned_type: 'user' },
            },
            { kind: 'text', text: ' hi' },
        ]);
        expect(msg.thread_ref).toEqual({
            selfChannelMessageId: 'om_msg2',
            replyToChannelMessageId: 'om_parent',
            rootChannelMessageId: 'om_root',
            inThread: true,
        });
    });

    it('splits Lark mention placeholders into structured mention items by key', () => {
        const msg = larkInbound.parse(mixedMentionTextEvent()) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(JSON.stringify(msg.content)).not.toContain('@_user_');
        expect(msg.content).toEqual([
            {
                kind: 'mention',
                id: 'on_bot1',
                label: '赤尾',
                meta: {
                    open_id: 'ou_bot1',
                    union_id: 'on_bot1',
                    mentioned_type: 'bot',
                    bot_app_id: 'cli_bot1',
                },
            },
            { kind: 'text', text: ' ' },
            {
                kind: 'mention',
                id: 'on_bot2',
                label: '绫奈',
                meta: {
                    open_id: 'ou_bot2',
                    union_id: 'on_bot2',
                    mentioned_type: 'bot',
                    bot_app_id: 'cli_bot2',
                },
            },
            { kind: 'text', text: ' 你俩认识' },
            {
                kind: 'mention',
                id: 'on_human',
                label: '原智鸿',
                meta: {
                    open_id: 'ou_human',
                    union_id: 'on_human',
                    mentioned_type: 'user',
                },
            },
            { kind: 'text', text: ' 么' },
        ]);
    });

    it('uses mention id as display fallback without changing identity', () => {
        const event = groupMentionEvent(['on_target']);
        event.message.content = JSON.stringify({ text: '@_user_1 你好' });
        event.message.mentions = [
            {
                key: '@_user_1',
                id: { union_id: 'on_target', open_id: 'ou_target' },
                name: '',
                mentioned_type: 'user',
            },
        ];
        const msg = larkInbound.parse(event) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            {
                kind: 'mention',
                id: 'on_target',
                label: 'on_target',
                meta: {
                    open_id: 'ou_target',
                    union_id: 'on_target',
                    mentioned_type: 'user',
                },
            },
            { kind: 'text', text: ' 你好' },
        ]);
    });

    it('image -> image content item', () => {
        const msg = larkInbound.parse(eventOfType('image', { image_key: 'img_x' })) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([{ kind: 'image', key: 'img_x' }]);
    });

    it('sticker -> sticker content item', () => {
        const msg = larkInbound.parse(eventOfType('sticker', { file_key: 'stk_1' })) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([{ kind: 'sticker', key: 'stk_1' }]);
    });

    it('post (rich text) -> mixed text/image items', () => {
        const post = {
            content: [
                [
                    { tag: 'text', text: 'hello ' },
                    { tag: 'img', image_key: 'img_in_post' },
                ],
                [{ tag: 'text', text: 'world' }],
            ],
        };
        const msg = larkInbound.parse(eventOfType('post', post)) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'text', text: 'hello ' },
            { kind: 'image', key: 'img_in_post' },
            { kind: 'text', text: 'world' },
        ]);
    });

    it('post at node -> structured mention item matched by lark ids', () => {
        const event = eventOfType('post', {
            content: [
                [
                    { tag: 'text', text: 'hello ' },
                    { tag: 'at', user_id: 'on_alice' },
                    { tag: 'text', text: ' end' },
                ],
            ],
        });
        event.message.mentions = [
            {
                key: '@_user_1',
                id: { union_id: 'on_alice', open_id: 'ou_alice' },
                name: 'Alice',
                mentioned_type: 'user',
            },
        ];

        const msg = larkInbound.parse(event) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'text', text: 'hello ' },
            {
                kind: 'mention',
                id: 'on_alice',
                label: 'Alice',
                meta: {
                    open_id: 'ou_alice',
                    union_id: 'on_alice',
                    mentioned_type: 'user',
                },
            },
            { kind: 'text', text: ' end' },
        ]);
    });

    it('media -> file content item carrying media meta', () => {
        const msg = larkInbound.parse(
            eventOfType('media', {
                file_key: 'media_k',
                image_key: 'thumb_k',
                file_name: 'v.mp4',
                duration: 1234,
            }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            {
                kind: 'file',
                key: 'media_k',
                meta: {
                    image_key: 'thumb_k',
                    file_name: 'v.mp4',
                    duration: 1234,
                    lark_type: 'media',
                },
            },
        ]);
    });

    it('file -> file content item carrying file meta', () => {
        const msg = larkInbound.parse(
            eventOfType('file', { file_key: 'file_k', file_name: 'doc.pdf' }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'file', key: 'file_k', meta: { file_name: 'doc.pdf', lark_type: 'file' } },
        ]);
    });

    it('audio -> audio content item carrying duration meta', () => {
        const msg = larkInbound.parse(
            eventOfType('audio', { file_key: 'audio_k', duration: 999 }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'audio', key: 'audio_k', meta: { duration: 999 } },
        ]);
    });

    it('unknown type -> unsupported content item preserving original_type', () => {
        const msg = larkInbound.parse(eventOfType('weird_type', { foo: 'bar' })) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([
            { kind: 'unsupported', text: '[weird_type]', meta: { original_type: 'weird_type' } },
        ]);
    });

    it('merge_forward -> unsupported preserving original_type', () => {
        const msg = larkInbound.parse(eventOfType('merge_forward', { foo: 1 })) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content[0].kind).toBe('unsupported');
        expect((msg.content[0] as { meta: Record<string, unknown> }).meta.original_type).toBe(
            'merge_forward',
        );
    });

    it('returns null when message missing (non-message event)', () => {
        const ev = { app_id: 'x', sender: { sender_id: {} } } as unknown as LarkReceiveMessage;
        expect(larkInbound.parse(ev)).toBeNull();
    });
});
