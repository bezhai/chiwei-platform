import { describe, it, expect } from 'bun:test';
import {
    assertValidInboundMessage,
    type InboundMessage,
} from '@core/channels/contracts';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import { qqInbound, QQ_CHANNEL, QQ_SELF_MENTION_TARGET } from './inbound';

function directText(over: Partial<CustomInboundMessage> = {}): CustomInboundMessage {
    return {
        botName: 'chiwei-qq',
        chatType: 'direct',
        conversationId: 'c2c_conv_1',
        senderId: 'user_open_1',
        senderName: '主人',
        text: 'hello chiwei',
        messageId: 'qq_msg_1',
        timestamp: '2026-06-27T10:00:00.000Z',
        ...over,
    };
}

function groupMsg(over: Partial<CustomInboundMessage> = {}): CustomInboundMessage {
    return {
        botName: 'chiwei-qq',
        chatType: 'group',
        conversationId: 'group_1',
        senderId: 'member_open_1',
        senderName: '群友',
        text: '@chiwei 在吗',
        messageId: 'qq_msg_2',
        timestamp: '2026-06-27T10:01:00.000Z',
        ...over,
    };
}

describe('qqInbound.handleHandshake', () => {
    it('returns null (handshake/verification done at the gateway)', () => {
        expect(qqInbound.handleHandshake({ anything: true })).toBeNull();
    });
});

describe('qqInbound.verify', () => {
    it('is constant-true (signature verification done at the gateway)', () => {
        expect(qqInbound.verify({})).toBe(true);
    });
});

describe('qqInbound.parse', () => {
    it('maps a direct custom message to a valid InboundMessage with scope=direct', () => {
        const msg = qqInbound.parse(directText()) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.channel).toBe(QQ_CHANNEL);
        expect(msg.bot_name).toBe('chiwei-qq');
        expect(msg.channel_message_id).toBe('qq_msg_1');
        expect(msg.channel_chat_id).toBe('c2c_conv_1');
        expect(msg.channel_user_id).toBe('user_open_1');
        expect(msg.conversation_scope).toBe('direct');
        expect(msg.thread_ref).toBeNull();
        expect(msg.addressing_hints).toEqual([]);
        expect(msg.content).toEqual([{ kind: 'text', text: 'hello chiwei' }]);
        expect(msg.received_at).toBe(Date.parse('2026-06-27T10:00:00.000Z'));
    });

    it('maps a group custom message with scope=group', () => {
        const msg = qqInbound.parse(groupMsg()) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.conversation_scope).toBe('group');
        expect(msg.channel_chat_id).toBe('group_1');
        expect(msg.channel_user_id).toBe('member_open_1');
    });

    it('carries the sender display name from a group custom message', () => {
        const msg = qqInbound.parse(groupMsg({ senderName: '群友' })) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.senderName).toBe('群友');
    });

    it('leaves senderName undefined when the gateway gives none (e.g. direct chat)', () => {
        const msg = qqInbound.parse(directText({ senderName: undefined })) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.senderName).toBeUndefined();
    });

    it('maps isSelf mention to the self addressing-hint sentinel; non-self mentions kept by id', () => {
        const msg = qqInbound.parse(
            groupMsg({
                mentions: [
                    { memberId: 'member_open_x', name: '别人', isSelf: false },
                    { memberId: 'bot_member', name: 'chiwei', isBot: true, isSelf: true },
                ],
            }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.addressing_hints).toEqual([
            { targetId: 'member_open_x' },
            { targetId: QQ_SELF_MENTION_TARGET },
        ]);
    });

    it('threads off a quote: replyToChannelMessageId comes from quote.messageId', () => {
        const msg = qqInbound.parse(
            directText({ quote: { messageId: 'quoted_qq_msg' } }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.thread_ref).toEqual({ replyToChannelMessageId: 'quoted_qq_msg' });
    });

    it('image attachment -> image content item keyed by url', () => {
        const msg = qqInbound.parse(
            directText({
                text: '',
                attachments: [{ contentType: 'image/png', url: 'https://qq.cdn/a.png' }],
            }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content).toEqual([{ kind: 'image', key: 'https://qq.cdn/a.png' }]);
    });

    it('text + image -> both content items, text first', () => {
        const msg = qqInbound.parse(
            directText({
                text: '看图',
                attachments: [{ contentType: 'image/jpeg', url: 'https://qq.cdn/b.jpg' }],
            }),
        ) as InboundMessage;
        expect(msg.content).toEqual([
            { kind: 'text', text: '看图' },
            { kind: 'image', key: 'https://qq.cdn/b.jpg' },
        ]);
    });

    it('audio attachment -> audio content item', () => {
        const msg = qqInbound.parse(
            directText({
                text: '',
                attachments: [
                    { contentType: 'audio/silk', url: 'https://qq.cdn/v.silk', asrText: '你好' },
                ],
            }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content[0].kind).toBe('audio');
        expect((msg.content[0] as { key: string }).key).toBe('https://qq.cdn/v.silk');
    });

    it('non-image/audio attachment -> file content item carrying filename meta', () => {
        const msg = qqInbound.parse(
            directText({
                text: '',
                attachments: [
                    { contentType: 'application/pdf', url: 'https://qq.cdn/d.pdf', filename: 'd.pdf' },
                ],
            }),
        ) as InboundMessage;
        assertValidInboundMessage(msg);
        expect(msg.content[0].kind).toBe('file');
        expect((msg.content[0] as { key: string }).key).toBe('https://qq.cdn/d.pdf');
    });

    it('returns null for an empty message (no text, no attachments)', () => {
        expect(qqInbound.parse(directText({ text: '' }))).toBeNull();
    });

    it('returns null when messageId missing (non-message payload)', () => {
        expect(
            qqInbound.parse({ text: 'x' } as unknown as CustomInboundMessage),
        ).toBeNull();
    });
});
