// chat.request 上那两个只有飞书答得出来的字段。
//
// persona_ids 是群聊唯一的应答者来源：agent-service 那侧群聊拿它决定谁开口，空数组
// 就是不回复。所以这里每一条都不是可选的细节。

import { describe, expect, it } from 'bun:test';

import type { RuleMessage } from '@inner/shared/rules';

import { larkChatRequestEnricher } from './chat-request';

function message(mentionedUserIds: string[]): RuleMessage {
    return {
        channel: 'lark',
        botName: 'chiwei',
        commonUserId: 'cu_sender',
        commonConversationId: 'cc_1',
        commonMessageId: 'cm_1',
        commonRootMessageId: undefined,
        isDirect: false,
        botCommonUserId: 'cu_bot_chiwei',
        mentionedUserIds,
        createTime: 0,
        clearText: () => '',
        text: () => '',
        withoutEmojiText: () => '',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
    };
}

const personas: Record<string, string> = {
    cu_bot_chiwei: 'p_chiwei',
    cu_bot_second: 'p_second',
    // 同一个人设挂着两个 bot：去重要按 persona_id，不是按被 @ 的人。
    cu_bot_twin: 'p_chiwei',
};

const enrich = larkChatRequestEnricher((commonUserId) => personas[commonUserId]);

describe('persona_ids', () => {
    it('把被 @ 的已注册 bot 收敛成它们的人设 id，按正文里的顺序', () => {
        expect(enrich(message(['cu_bot_second', 'cu_bot_chiwei'])).personaIds).toEqual([
            'p_second',
            'p_chiwei',
        ]);
    });

    it('被 @ 的真人不贡献人设 —— 他们不是应答者', () => {
        expect(enrich(message(['cu_human', 'cu_bot_chiwei', 'cu_another_human'])).personaIds).toEqual(
            ['p_chiwei'],
        );
    });

    it('同一个人设只算一次', () => {
        expect(enrich(message(['cu_bot_chiwei', 'cu_bot_twin'])).personaIds).toEqual(['p_chiwei']);
    });

    // 私聊没有 @，这时空数组是对的：agent-service 私聊本来就不看 persona_ids。
    it('没人被 @ 时是空数组', () => {
        expect(enrich(message([])).personaIds).toEqual([]);
    });
});

describe('is_canary', () => {
    // 恒 false，理由见实现里的长注释：agent-service 的 ChatTrigger 上没有这个字段，
    // MQ source 在反序列化前按 model_fields 过滤，它被静默丢弃。
    it('恒为 false', () => {
        expect(enrich(message(['cu_bot_chiwei'])).isCanary).toBe(false);
        expect(enrich(message([])).isCanary).toBe(false);
    });
});
