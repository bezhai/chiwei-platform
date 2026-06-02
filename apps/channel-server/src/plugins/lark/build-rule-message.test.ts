import { describe, it, expect } from 'bun:test';

import { buildLarkRuleMessage } from './build-rule-message';
import { larkContextStore } from './lark-context-store';
import type { Message } from '@core/models/message';

// B2：buildLarkRuleMessage 从 core/rules/rule-message.ts 搬进 lark 插件。
// 它做两件事，缺一不可：
//   1. 产出平台无关 RuleMessage（飞书字段委托 Message 等价方法，行为零变化），
//      且**绝不**在 RuleMessage 上挂任何飞书对象（无 channelContext / larkMessage）。
//   2. 把飞书 Message put 进 lark 私有 store，key=全局 commonMessageId，供
//      lark 谓词/handler 后续 get 取回 —— core 永远看不到 Message。

function fakeLark(over: Partial<Record<string, unknown>> = {}): Message {
    return {
        isP2P: () => false,
        createTime: '1700000000000',
        clearText: () => '余额',
        text: () => '余额 text',
        withMentionText: () => '@bot 余额',
        withoutEmojiText: () => '余额',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => ['img-1'],
        ...over,
    } as unknown as Message;
}

const ids = {
    botName: 'bot-x',
    commonUserId: 'GU',
    commonConversationId: 'GC',
    commonMessageId: 'GM',
    commonRootMessageId: 'GR',
    botCommonUserId: 'BOT-U',
    mentionedUserIds: ['BOT-U', 'OTHER-U'],
};

describe('buildLarkRuleMessage (lark plugin)', () => {
    it('produces a platform-neutral RuleMessage with NO lark object side-channel', () => {
        const rm = buildLarkRuleMessage(fakeLark(), ids);
        expect(rm.channel).toBe('lark');
        expect(rm.commonUserId).toBe('GU');
        expect(rm.commonConversationId).toBe('GC');
        expect(rm.commonMessageId).toBe('GM');
        expect(rm.commonRootMessageId).toBe('GR');
        expect(rm.botCommonUserId).toBe('BOT-U');
        expect(rm.mentionedUserIds).toEqual(['BOT-U', 'OTHER-U']);
        // 灵魂检查：RuleMessage 不再携带任何飞书逃生口。
        expect('channelContext' in rm).toBe(false);
        // RuleMessage 与 Record<string, unknown> 无足够结构重叠，按 TS 提示经
        // unknown 中转再断言成索引字典，用于运行期确认渠道私有逃生口不存在。
        expect((rm as unknown as Record<string, unknown>).larkMessage).toBeUndefined();
        larkContextStore.clear(rm);
    });

    it('neutral text/media tools delegate to the lark Message (behaviour unchanged)', () => {
        const rm = buildLarkRuleMessage(fakeLark(), ids);
        expect(rm.clearText()).toBe('余额');
        expect(rm.text()).toBe('余额 text');
        expect(rm.withMentionText()).toBe('@bot 余额');
        expect(rm.withoutEmojiText()).toBe('余额');
        expect(rm.isTextOnly()).toBe(true);
        expect(rm.isStickerOnly()).toBe(false);
        expect(rm.imageKeys()).toEqual(['img-1']);
        expect(rm.isDirect).toBe(false);
        expect(rm.createTime).toBe(1700000000000);
        larkContextStore.clear(rm);
    });

    it('puts the lark Message into the plugin store keyed by bot and commonMessageId', () => {
        const lark = fakeLark();
        const rm = buildLarkRuleMessage(lark, ids);
        // 飞书谓词/handler 经全局 commonMessageId 从 store 取回原 Message。
        expect(larkContextStore.get(rm)).toBe(lark);
        larkContextStore.clear(rm);
    });

    it('separates the same commonMessageId across bots', () => {
        const larkA = fakeLark({ messageId: 'raw-a' });
        const larkB = fakeLark({ messageId: 'raw-b' });
        const rmA = buildLarkRuleMessage(larkA, { ...ids, botName: 'bot-a' });
        const rmB = buildLarkRuleMessage(larkB, { ...ids, botName: 'bot-b' });

        larkContextStore.clear(rmA);
        expect(larkContextStore.get(rmB)).toBe(larkB);
        larkContextStore.clear(rmB);
    });
});
