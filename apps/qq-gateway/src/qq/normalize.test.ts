import { describe, it, expect } from 'bun:test';
import { validateCustomInboundMessage } from '@inner/shared/protocols';
import { normalizeQQEvent } from './normalize';

const CTX = { botName: 'chiwei' };

describe('normalizeQQEvent: C2C (private) message', () => {
    const d = {
        author: { id: 'aid', union_openid: 'u-union', user_openid: 'user-openid-1' },
        content: '你好赤尾',
        id: 'C2C_MSGID_1',
        timestamp: '2026-06-27T10:00:00+08:00',
        attachments: [
            {
                content_type: 'image/png',
                url: 'https://q.qq/img.png',
                filename: 'img.png',
                size: 1024,
                width: 100,
                height: 200,
            },
        ],
    };

    it('maps C2C_MESSAGE_CREATE into a direct CustomInboundMessage', () => {
        const msg = normalizeQQEvent('C2C_MESSAGE_CREATE', d, CTX);
        expect(msg).not.toBeNull();
        expect(msg!.botName).toBe('chiwei');
        expect(msg!.chatType).toBe('direct');
        expect(msg!.conversationId).toBe('user-openid-1');
        expect(msg!.senderId).toBe('user-openid-1');
        expect(msg!.text).toBe('你好赤尾');
        expect(msg!.messageId).toBe('C2C_MSGID_1');
        expect(msg!.timestamp).toBe('2026-06-27T10:00:00+08:00');
        expect(msg!.mentions).toBeUndefined();
    });

    it('maps attachment fields snake_case -> camelCase', () => {
        const msg = normalizeQQEvent('C2C_MESSAGE_CREATE', d, CTX)!;
        expect(msg.attachments).toHaveLength(1);
        expect(msg.attachments![0]).toMatchObject({
            contentType: 'image/png',
            url: 'https://q.qq/img.png',
            filename: 'img.png',
            size: 1024,
            width: 100,
            height: 200,
        });
    });

    it('produces output that passes the wire validator', () => {
        const msg = normalizeQQEvent('C2C_MESSAGE_CREATE', d, CTX)!;
        expect(() => validateCustomInboundMessage(msg)).not.toThrow();
    });
});

describe('normalizeQQEvent: group @ message', () => {
    const d = {
        author: { id: 'gaid', member_openid: 'member-openid-9', username: '路人甲', bot: false },
        content: '<@bot-self> 在吗',
        id: 'GROUP_MSGID_2',
        timestamp: '2026-06-27T11:00:00+08:00',
        group_id: 'gid',
        group_openid: 'group-openid-7',
        mentions: [
            { scope: 'single', member_openid: 'bot-self', nickname: '赤尾', bot: true, is_you: true },
            { scope: 'single', member_openid: 'other-1', nickname: '老王', bot: false, is_you: false },
        ],
        attachments: [
            { content_type: 'audio/silk', url: 'https://q.qq/v.silk', voice_wav_url: 'https://q.qq/v.wav', asr_refer_text: '语音内容' },
        ],
    };

    it('maps GROUP_AT_MESSAGE_CREATE into a group CustomInboundMessage', () => {
        const msg = normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)!;
        expect(msg.chatType).toBe('group');
        expect(msg.conversationId).toBe('group-openid-7');
        expect(msg.senderId).toBe('member-openid-9');
        expect(msg.senderName).toBe('路人甲');
        expect(msg.senderIsBot).toBe(false);
        expect(msg.messageId).toBe('GROUP_MSGID_2');
        expect(msg.text).toBe('<@bot-self> 在吗');
    });

    it('preserves mentions with isSelf flag for the @bot mention', () => {
        const msg = normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)!;
        expect(msg.mentions).toHaveLength(2);
        const self = msg.mentions!.find((m) => m.isSelf);
        expect(self).toBeDefined();
        expect(self!.memberId).toBe('bot-self');
        expect(self!.name).toBe('赤尾');
        expect(self!.isBot).toBe(true);
        const other = msg.mentions!.find((m) => m.memberId === 'other-1');
        expect(other!.isSelf).toBe(false);
    });

    it('maps voice attachment fields (voice_wav_url, asr_refer_text)', () => {
        const msg = normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)!;
        expect(msg.attachments![0]).toMatchObject({
            contentType: 'audio/silk',
            url: 'https://q.qq/v.silk',
            voiceWavUrl: 'https://q.qq/v.wav',
            asrText: '语音内容',
        });
    });

    it('also handles GROUP_MESSAGE_CREATE the same way', () => {
        const msg = normalizeQQEvent('GROUP_MESSAGE_CREATE', d, CTX)!;
        expect(msg.chatType).toBe('group');
    });

    it('produces output that passes the wire validator', () => {
        const msg = normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)!;
        expect(() => validateCustomInboundMessage(msg)).not.toThrow();
    });
});

describe('normalizeQQEvent: drops empty-url attachments (no empty image key downstream)', () => {
    it('C2C: a sole attachment with empty url is omitted entirely', () => {
        const d = {
            author: { user_openid: 'u1' },
            content: '看图',
            id: 'M_EMPTY_1',
            timestamp: '2026-06-27T10:00:00+08:00',
            attachments: [{ content_type: 'image/png', url: '' }],
        };
        const msg = normalizeQQEvent('C2C_MESSAGE_CREATE', d, CTX)!;
        expect(msg).not.toBeNull();
        expect(msg.attachments).toBeUndefined();
        // the wire validator (which now rejects empty url) must accept this output
        expect(() => validateCustomInboundMessage(msg)).not.toThrow();
    });

    it('group: keeps only attachments that carry a non-empty url', () => {
        const d = {
            author: { member_openid: 'm9' },
            content: '看图',
            id: 'M_EMPTY_2',
            group_openid: 'g7',
            timestamp: '2026-06-27T11:00:00+08:00',
            attachments: [
                { content_type: 'image/png', url: '' },
                { content_type: 'image/jpeg', url: 'https://q.qq/ok.jpg' },
            ],
        };
        const msg = normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)!;
        expect(msg.attachments).toHaveLength(1);
        expect(msg.attachments![0].url).toBe('https://q.qq/ok.jpg');
        expect(() => validateCustomInboundMessage(msg)).not.toThrow();
    });
});

describe('normalizeQQEvent: non-relayed events', () => {
    it('returns null for system events like GROUP_ADD_ROBOT', () => {
        expect(normalizeQQEvent('GROUP_ADD_ROBOT', { group_openid: 'g', op_member_openid: 'm' }, CTX)).toBeNull();
    });

    it('returns null for unknown event types', () => {
        expect(normalizeQQEvent('SOMETHING_ELSE', {}, CTX)).toBeNull();
    });
});

describe('normalizeQQEvent: fail-loud on missing bare ids (drop, do not invent "unknown")', () => {
    it('C2C without user_openid → null', () => {
        const d = { author: { id: 'aid' }, content: 'hi', id: 'M1', timestamp: '2026-06-27T10:00:00+08:00' };
        expect(normalizeQQEvent('C2C_MESSAGE_CREATE', d, CTX)).toBeNull();
    });

    it('C2C without messageId → null', () => {
        const d = { author: { user_openid: 'u1' }, content: 'hi', timestamp: '2026-06-27T10:00:00+08:00' };
        expect(normalizeQQEvent('C2C_MESSAGE_CREATE', d, CTX)).toBeNull();
    });

    it('group without group_openid → null', () => {
        const d = {
            author: { member_openid: 'm9' },
            content: 'hi',
            id: 'M2',
            timestamp: '2026-06-27T11:00:00+08:00',
        };
        expect(normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)).toBeNull();
    });

    it('group without member_openid → null', () => {
        const d = {
            author: { username: '路人' },
            content: 'hi',
            id: 'M3',
            group_openid: 'g7',
            timestamp: '2026-06-27T11:00:00+08:00',
        };
        expect(normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)).toBeNull();
    });

    it('group without messageId → null', () => {
        const d = {
            author: { member_openid: 'm9' },
            content: 'hi',
            group_openid: 'g7',
            timestamp: '2026-06-27T11:00:00+08:00',
        };
        expect(normalizeQQEvent('GROUP_AT_MESSAGE_CREATE', d, CTX)).toBeNull();
    });
});
