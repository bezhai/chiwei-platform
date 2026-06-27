import { describe, test, expect } from 'bun:test';
import {
    validateCustomInboundMessage,
    validateCustomOutboundMessage,
    validateCustomOutboundResult,
    type CustomInboundMessage,
    type CustomOutboundMessage,
    type CustomOutboundResult,
} from './index';

// A fully-populated, valid inbound message (every field exercised).
function fullInbound(): CustomInboundMessage {
    return {
        botName: 'chiwei-qq',
        chatType: 'group',
        conversationId: 'group_001',
        senderId: 'member_001',
        senderName: 'Bob',
        senderIsBot: false,
        text: '@赤尾 在吗',
        messageId: 'msg_20001',
        timestamp: '2026-06-27T10:05:00+08:00',
        attachments: [
            {
                contentType: 'image/png',
                url: 'https://example.com/a.png',
                filename: 'a.png',
                size: 1234,
                width: 800,
                height: 600,
                voiceWavUrl: 'https://example.com/a.wav',
                asrText: '你好',
            },
        ],
        mentions: [
            {
                id: 'm1',
                userId: 'u1',
                memberId: 'bot_001',
                name: '赤尾',
                isBot: true,
                isSelf: true,
            },
        ],
        quote: {
            refId: 'ref1',
            messageId: 'msg_19999',
            content: '上一条',
            senderId: 'member_002',
            senderName: 'Carol',
            attachments: [{ contentType: 'image/jpeg', url: 'https://example.com/b.jpg' }],
        },
        raw: { platform: 'qq', evt: { foo: 'bar' } },
    };
}

// Minimal valid inbound: only required fields present.
function minimalInbound(): CustomInboundMessage {
    return {
        botName: 'chiwei-qq',
        chatType: 'direct',
        conversationId: 'user_001',
        senderId: 'user_001',
        text: '你好',
        messageId: 'msg_10001',
        timestamp: '2026-06-27T10:00:00+08:00',
    };
}

function fullOutbound(): CustomOutboundMessage {
    return {
        botName: 'chiwei-qq',
        chatType: 'group',
        conversationId: 'group_001',
        replyToMessageId: 'msg_20001',
        text: '在的',
        mediaUrls: ['https://example.com/c.png'],
        partIndex: 0,
        isLast: false,
        idempotencyKey: 'gid_abc#0',
        raw: { note: 'x' },
    };
}

// Minimal valid outbound: only required fields present.
function minimalOutbound(): CustomOutboundMessage {
    return {
        botName: 'chiwei-qq',
        chatType: 'direct',
        conversationId: 'user_001',
        idempotencyKey: 'gid_abc#0',
    };
}

describe('validateCustomInboundMessage', () => {
    test('accepts a fully-populated message and returns it unchanged', () => {
        const msg = fullInbound();
        const out = validateCustomInboundMessage(msg);
        expect(out).toEqual(msg);
    });

    test('accepts a minimal message with only required fields', () => {
        const msg = minimalInbound();
        expect(validateCustomInboundMessage(msg)).toEqual(msg);
    });

    test('survives a JSON serialize/parse round-trip', () => {
        const msg = fullInbound();
        const roundTripped = JSON.parse(JSON.stringify(msg));
        expect(validateCustomInboundMessage(roundTripped)).toEqual(msg);
    });

    test('throws when input is not an object', () => {
        expect(() => validateCustomInboundMessage(null)).toThrow();
        expect(() => validateCustomInboundMessage('nope')).toThrow();
        expect(() => validateCustomInboundMessage(42)).toThrow();
    });

    test('throws when required botName is missing', () => {
        const { botName, ...rest } = minimalInbound();
        expect(() => validateCustomInboundMessage(rest)).toThrow(/botName/);
    });

    test('throws when required messageId is missing', () => {
        const { messageId, ...rest } = minimalInbound();
        expect(() => validateCustomInboundMessage(rest)).toThrow(/messageId/);
    });

    test('throws when conversationId is missing', () => {
        const { conversationId, ...rest } = minimalInbound();
        expect(() => validateCustomInboundMessage(rest)).toThrow(/conversationId/);
    });

    test('throws when text is missing', () => {
        const { text, ...rest } = minimalInbound();
        expect(() => validateCustomInboundMessage(rest)).toThrow(/text/);
    });

    test('throws when timestamp is missing', () => {
        const { timestamp, ...rest } = minimalInbound();
        expect(() => validateCustomInboundMessage(rest)).toThrow(/timestamp/);
    });

    test('throws when a required field has wrong type', () => {
        const bad = { ...minimalInbound(), messageId: 12345 };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/messageId/);
    });

    test('throws when chatType is an illegal value', () => {
        const bad = { ...minimalInbound(), chatType: 'channel' };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/chatType/);
    });

    test('throws when an optional field has wrong type', () => {
        const bad = { ...minimalInbound(), senderIsBot: 'yes' };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/senderIsBot/);
    });

    test('throws when attachments is not an array', () => {
        const bad = { ...minimalInbound(), attachments: {} };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/attachments/);
    });

    test('throws when an attachment lacks required url', () => {
        const bad = { ...minimalInbound(), attachments: [{ contentType: 'image/png' }] };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/url/);
    });

    test('throws when an attachment url is an empty string', () => {
        const bad = { ...minimalInbound(), attachments: [{ contentType: 'image/png', url: '' }] };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/url/);
    });

    test('throws when a mention field has wrong type', () => {
        const bad = { ...minimalInbound(), mentions: [{ isSelf: 'true' }] };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/isSelf/);
    });

    test('throws when quote is not an object', () => {
        const bad = { ...minimalInbound(), quote: 'oops' };
        expect(() => validateCustomInboundMessage(bad)).toThrow(/quote/);
    });
});

describe('validateCustomOutboundMessage', () => {
    test('accepts a fully-populated message and returns it unchanged', () => {
        const msg = fullOutbound();
        expect(validateCustomOutboundMessage(msg)).toEqual(msg);
    });

    test('accepts a minimal message with only required fields', () => {
        const msg = minimalOutbound();
        expect(validateCustomOutboundMessage(msg)).toEqual(msg);
    });

    test('survives a JSON serialize/parse round-trip', () => {
        const msg = fullOutbound();
        const roundTripped = JSON.parse(JSON.stringify(msg));
        expect(validateCustomOutboundMessage(roundTripped)).toEqual(msg);
    });

    test('throws when input is not an object', () => {
        expect(() => validateCustomOutboundMessage(null)).toThrow();
        expect(() => validateCustomOutboundMessage([])).toThrow();
    });

    test('throws when required botName is missing', () => {
        const { botName, ...rest } = minimalOutbound();
        expect(() => validateCustomOutboundMessage(rest)).toThrow(/botName/);
    });

    test('throws when required idempotencyKey is missing', () => {
        const { idempotencyKey, ...rest } = minimalOutbound();
        expect(() => validateCustomOutboundMessage(rest)).toThrow(/idempotencyKey/);
    });

    test('throws when conversationId is missing', () => {
        const { conversationId, ...rest } = minimalOutbound();
        expect(() => validateCustomOutboundMessage(rest)).toThrow(/conversationId/);
    });

    test('throws when chatType is an illegal value', () => {
        const bad = { ...minimalOutbound(), chatType: 'guild' };
        expect(() => validateCustomOutboundMessage(bad)).toThrow(/chatType/);
    });

    test('throws when text has wrong type', () => {
        const bad = { ...minimalOutbound(), text: 123 };
        expect(() => validateCustomOutboundMessage(bad)).toThrow(/text/);
    });

    test('throws when replyToMessageId has wrong type', () => {
        const bad = { ...minimalOutbound(), replyToMessageId: 999 };
        expect(() => validateCustomOutboundMessage(bad)).toThrow(/replyToMessageId/);
    });

    test('throws when partIndex has wrong type', () => {
        const bad = { ...minimalOutbound(), partIndex: '0' };
        expect(() => validateCustomOutboundMessage(bad)).toThrow(/partIndex/);
    });

    test('throws when isLast has wrong type', () => {
        const bad = { ...minimalOutbound(), isLast: 'no' };
        expect(() => validateCustomOutboundMessage(bad)).toThrow(/isLast/);
    });

    test('throws when mediaUrls is not an array of strings', () => {
        const bad = { ...minimalOutbound(), mediaUrls: [1, 2] };
        expect(() => validateCustomOutboundMessage(bad)).toThrow(/mediaUrls/);
    });
});

describe('validateCustomOutboundResult', () => {
    test('accepts a success result with messageId', () => {
        const ok: CustomOutboundResult = { sent: true, messageId: 'qq_msg_1' };
        expect(validateCustomOutboundResult(ok)).toEqual(ok);
    });

    test('accepts a drop/fail result with reason', () => {
        const dropped: CustomOutboundResult = { sent: false, reason: 'active_send' };
        expect(validateCustomOutboundResult(dropped)).toEqual(dropped);
    });

    test('accepts a bare sent:false with no reason', () => {
        expect(validateCustomOutboundResult({ sent: false })).toEqual({ sent: false });
    });

    test('survives a JSON serialize/parse round-trip', () => {
        const ok: CustomOutboundResult = { sent: true, messageId: 'qq_msg_2' };
        expect(validateCustomOutboundResult(JSON.parse(JSON.stringify(ok)))).toEqual(ok);
    });

    test('throws when input is not an object', () => {
        expect(() => validateCustomOutboundResult(null)).toThrow();
        expect(() => validateCustomOutboundResult('nope')).toThrow();
    });

    test('throws when required sent is missing', () => {
        expect(() => validateCustomOutboundResult({ messageId: 'x' })).toThrow(/sent/);
    });

    test('throws when sent has wrong type', () => {
        expect(() => validateCustomOutboundResult({ sent: 'yes' })).toThrow(/sent/);
    });

    test('throws when messageId has wrong type', () => {
        expect(() => validateCustomOutboundResult({ sent: true, messageId: 123 })).toThrow(
            /messageId/,
        );
    });

    test('throws when reason has wrong type', () => {
        expect(() => validateCustomOutboundResult({ sent: false, reason: 1 })).toThrow(/reason/);
    });

    test('throws when sent:true but messageId is missing', () => {
        expect(() => validateCustomOutboundResult({ sent: true })).toThrow(/messageId/);
    });

    test('throws when sent:true but messageId is empty', () => {
        expect(() => validateCustomOutboundResult({ sent: true, messageId: '' })).toThrow(
            /messageId/,
        );
    });

    test('accepts sent:true with a non-empty messageId', () => {
        const ok: CustomOutboundResult = { sent: true, messageId: 'x' };
        expect(validateCustomOutboundResult(ok)).toEqual(ok);
    });

    test('accepts sent:false with a reason and no messageId', () => {
        const dropped: CustomOutboundResult = { sent: false, reason: 'duplicate' };
        expect(validateCustomOutboundResult(dropped)).toEqual(dropped);
    });
});
