import { describe, it, expect } from 'bun:test';

import {
    type RuleMessage,
    type LarkRuleContext,
    requireLarkContext,
} from './rule-message';

// RuleMessage 是 InboundMessage 派生的平台无关视图 + 可选 channelContext。
// 平台无关部分必须支撑 runRules 的平台无关规则；飞书强绑的东西经
// channelContext 旁挂（LarkRuleContext），缺它时 lark-only handler fail-loud。

function neutralMsg(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'qq',
        botName: 'bot-x',
        internalUserId: 'U1',
        internalChatId: 'C1',
        internalMessageId: 'M1',
        internalRootId: undefined,
        isDirect: false,
        addressedTargetIds: [],
        createTime: 100,
        clearText: () => '',
        text: () => '',
        withMentionText: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        channelContext: undefined,
        ...over,
    };
}

describe('RuleMessage platform-neutral view', () => {
    it('carries channel + global ids + neutral text/media tools without any lark binding', () => {
        const m = neutralMsg({
            clearText: () => '余额',
            isTextOnly: () => true,
            addressedTargetIds: ['bot-union-1'],
            isDirect: true,
        });
        expect(m.channel).toBe('qq');
        expect(m.internalUserId).toBe('U1');
        expect(m.clearText()).toBe('余额');
        expect(m.isTextOnly()).toBe(true);
        expect(m.addressedTargetIds).toEqual(['bot-union-1']);
        expect(m.isDirect).toBe(true);
        expect(m.channelContext).toBeUndefined();
    });
});

describe('requireLarkContext fail-loud', () => {
    it('returns the LarkRuleContext when present', () => {
        const fakeLark = { messageId: 'lark-m' } as unknown;
        const ctx: LarkRuleContext = {
            channel: 'lark',
            larkMessage: fakeLark as never,
        };
        const m = neutralMsg({ channel: 'lark', channelContext: ctx });
        expect(requireLarkContext(m)).toBe(ctx);
    });

    it('throws (fail-loud, no silent skip) when a lark-only handler runs without channelContext', () => {
        const m = neutralMsg({ channel: 'lark', channelContext: undefined });
        expect(() => requireLarkContext(m)).toThrow(/lark/i);
    });

    it('throws when channelContext is for a different channel', () => {
        const ctx = {
            channel: 'qq',
            larkMessage: {} as never,
        } as unknown as LarkRuleContext;
        const m = neutralMsg({ channel: 'lark', channelContext: ctx });
        expect(() => requireLarkContext(m)).toThrow();
    });
});
