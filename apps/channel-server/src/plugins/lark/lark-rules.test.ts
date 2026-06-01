import { describe, it, expect, afterEach } from 'bun:test';

import { WhiteGroupCheck, IsAdmin } from './lark-rules';
import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';
import type { RuleMessage } from '@core/rules/rule-message';
import type { LarkBaseChatInfo } from 'infrastructure/dal/entities';

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
        addressedTargetIds: [],
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

function putLark(key: string, over: Partial<Record<string, unknown>>): Message {
    const m = over as unknown as Message;
    larkContextStore.put(key, m);
    return m;
}

afterEach(() => {
    larkContextStore.clear('GM');
});

describe('IsAdmin (lark, reads from plugin store)', () => {
    it('true when senderInfo.is_admin is true', () => {
        putLark('GM', { senderInfo: { is_admin: true } });
        expect(IsAdmin(rm())).toBe(true);
    });

    it('false when senderInfo.is_admin is false / missing', () => {
        putLark('GM', { senderInfo: { is_admin: false } });
        expect(IsAdmin(rm())).toBe(false);
        larkContextStore.clear('GM');
        putLark('GM', { senderInfo: undefined });
        expect(IsAdmin(rm())).toBe(false);
    });

    it('fail-loud when lark Message absent from store (no silent skip)', () => {
        expect(() => IsAdmin(rm({ commonMessageId: 'MISSING' }))).toThrow(/lark/i);
    });
});

describe('WhiteGroupCheck (lark, reads from plugin store)', () => {
    it('applies the predicate to basicChatInfo when present', () => {
        putLark('GM', {
            basicChatInfo: { permission_config: { open_repeat_message: true } },
        });
        const rule = WhiteGroupCheck(
            (info: LarkBaseChatInfo) => info.permission_config?.open_repeat_message ?? false,
        );
        expect(rule(rm())).toBe(true);
    });

    it('returns false when basicChatInfo missing (behaviour unchanged)', () => {
        putLark('GM', { basicChatInfo: undefined });
        const rule = WhiteGroupCheck(() => true);
        expect(rule(rm())).toBe(false);
    });
});
