import { describe, it, expect, afterEach } from 'bun:test';

import { WhiteGroupCheck, IsAdmin } from './lark-rules';
import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';
import type { RuleMessage } from '@core/rules/rule-message';
import type { MessageBasicChatInfo } from '@core/models/message-metadata';

// B2：飞书强绑谓词 WhiteGroupCheck / IsAdmin 从 core/rules/rule.ts 搬进
// plugins/lark。它们不再经 requireLarkContext 掏旁挂的 larkMessage，而是用
// 平台无关 RuleMessage 的 commonMessageId 从 lark 私有 store 取回飞书 Message
// 跑不变的判定逻辑。内部判定逻辑与改造前逐字一致，只改「飞书数据从哪来」。

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'lark',
        botName: 'bot-x',
        commonUserId: 'U1',
        commonConversationId: 'C1',
        commonMessageId: 'GM',
        commonRootMessageId: undefined,
        isDirect: false,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 0,
        clearText: () => '',
        text: () => '',
        withMentionText: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

function putLark(key: RuleMessage, over: Partial<Record<string, unknown>>): Message {
    const m = over as unknown as Message;
    larkContextStore.put(key, m);
    return m;
}

afterEach(() => {
    larkContextStore.clear(rm());
});

describe('IsAdmin (lark, reads from plugin store)', () => {
    it('true when senderInfo.is_admin is true', () => {
        const message = rm();
        putLark(message, { senderInfo: { is_admin: true } });
        expect(IsAdmin(message)).toBe(true);
    });

    it('false when senderInfo.is_admin is false / missing', () => {
        const message = rm();
        putLark(message, { senderInfo: { is_admin: false } });
        expect(IsAdmin(message)).toBe(false);
        larkContextStore.clear(message);
        putLark(message, { senderInfo: undefined });
        expect(IsAdmin(message)).toBe(false);
    });

    it('fail-loud when lark Message absent from store (no silent skip)', () => {
        expect(() => IsAdmin(rm({ commonMessageId: 'MISSING' }))).toThrow(/lark/i);
    });
});

describe('WhiteGroupCheck (lark, reads from plugin store)', () => {
    it('applies the predicate to basicChatInfo when present', () => {
        const message = rm();
        putLark(message, {
            basicChatInfo: { permission_config: { open_repeat_message: true } },
        });
        const rule = WhiteGroupCheck(
            (info: MessageBasicChatInfo) => info.permission_config?.open_repeat_message ?? false,
        );
        expect(rule(message)).toBe(true);
    });

    it('returns false when basicChatInfo missing (behaviour unchanged)', () => {
        const message = rm();
        putLark(message, { basicChatInfo: undefined });
        const rule = WhiteGroupCheck(() => true);
        expect(rule(message)).toBe(false);
    });
});
