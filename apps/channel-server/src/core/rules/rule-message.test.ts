import { describe, it, expect } from 'bun:test';

import type { RuleMessage } from './rule-message';

// RuleMessage 是 InboundMessage 派生的**纯平台无关视图**（B2 杀掉 #228 的
// larkMessage 旁挂之后）。它只承载平台无关字段（channel / 全局 common_*_id /
// is_direct / common mention list / bot common user id / createTime / 文本&媒体工具）。任何飞书原始
// 对象都不在 RuleMessage 上 —— 飞书数据全部走 lark 插件私有 context store。

function neutralMsg(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'qq',
        botName: 'bot-x',
        commonUserId: 'U1',
        commonConversationId: 'C1',
        commonMessageId: 'M1',
        commonRootMessageId: undefined,
        isDirect: false,
        botCommonUserId: 'BOT-U',
        mentionedUserIds: [],
        createTime: 100,
        clearText: () => '',
        text: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

describe('RuleMessage platform-neutral view', () => {
    it('carries channel + global ids + neutral text/media tools without any lark binding', () => {
        const m = neutralMsg({
            clearText: () => '余额',
            isTextOnly: () => true,
            botCommonUserId: 'BOT-U',
            mentionedUserIds: ['BOT-U', 'OTHER-U'],
            isDirect: true,
        });
        expect(m.channel).toBe('qq');
        expect(m.commonUserId).toBe('U1');
        expect(m.clearText()).toBe('余额');
        expect(m.isTextOnly()).toBe(true);
        expect(m.botCommonUserId).toBe('BOT-U');
        expect(m.mentionedUserIds).toEqual(['BOT-U', 'OTHER-U']);
        expect(m.isDirect).toBe(true);
    });

    it('has no lark side-channel field (no channelContext / larkMessage escape hatch)', () => {
        const m = neutralMsg();
        // 灵魂检查：core 的 RuleMessage 类型上根本没有取回飞书对象的逃生口。
        expect('channelContext' in m).toBe(false);
        // RuleMessage 与 Record<string, unknown> 无足够结构重叠，按 TS 提示经
        // unknown 中转再断言成索引字典，用于运行期确认渠道私有逃生口不存在。
        expect((m as unknown as Record<string, unknown>).larkMessage).toBeUndefined();
    });
});
