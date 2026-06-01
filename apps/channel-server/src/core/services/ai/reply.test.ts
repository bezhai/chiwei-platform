import { describe, it, expect, afterEach } from 'bun:test';

import {
    buildChatRequestPayload,
    setChatRequestEnricher,
    resetChatRequestEnricher,
} from './reply';
import type { RuleMessage } from 'core/rules/rule-message';

// makeTextReply 是 persona 文本主链路，决策五里唯一真正平台无关的规则。
// 它必须直接消费 RuleMessage 上已是全局 common_*_id 的身份字段（不再绕
// channel-binding context 退回飞书裸 ID）。ChatTrigger 带 channel + 全局 ID，
// agent-service 无感知透传，ChatResponseSegment 原路带回。
//
// B2：is_canary / persona_ids 是 channel 插件富化结果。core 的 reply.ts
// 不读任何飞书旁挂对象（#228 的 channelContext.larkMessage 逃生口已删）。
// 未注入时取中性默认（is_canary=false / persona_ids=[]）。

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'qq',
        botName: 'bot-q',
        commonUserId: 'GU',
        commonConversationId: 'GC',
        commonMessageId: 'GM',
        commonRootMessageId: 'GR',
        isDirect: true,
        addressedTargetIds: [],
        createTime: 1,
        clearText: () => 'hi',
        text: () => 'hi',
        withMentionText: () => 'hi',
        withoutEmojiText: () => 'hi',
        isTextOnly: () => true,
        isStickerOnly: () => false,
        stickerKey: () => '',
        imageKeys: () => [],
        ...over,
    };
}

afterEach(() => {
    resetChatRequestEnricher();
});

describe('buildChatRequestPayload (platform-neutral persona path)', () => {
    it('uses global common_*_id from RuleMessage, carries channel', () => {
        const p = buildChatRequestPayload(rm(), 'sess-1', 'bot-q', undefined);
        expect(p.channel).toBe('qq');
        expect(p.message_id).toBe('GM');
        expect(p.chat_id).toBe('GC');
        expect(p.user_id).toBe('GU');
        expect(p.root_id).toBe('GR');
        expect(p.is_p2p).toBe(true);
        expect(p.session_id).toBe('sess-1');
    });

    it('root_id falls back to message_id when no commonRootMessageId', () => {
        const p = buildChatRequestPayload(
            rm({ commonRootMessageId: undefined }),
            's',
            'b',
            undefined,
        );
        expect(p.root_id).toBe('GM');
    });

    it('no enricher registered: is_canary=false, persona_ids=[] (no platform binding leaked)', () => {
        const p = buildChatRequestPayload(rm({ channel: 'qq' }), 's', 'b', undefined);
        expect(p.is_canary).toBe(false);
        expect(p.persona_ids).toEqual([]);
    });

    it('registered enricher supplies is_canary + persona_ids', () => {
        setChatRequestEnricher((m) => {
            if (m.channel !== 'lark') return { isCanary: false, personaIds: [] };
            return { isCanary: true, personaIds: ['persona-1', 'persona-2'] };
        });
        const p = buildChatRequestPayload(
            rm({ channel: 'lark', isDirect: false }),
            's',
            'b',
            undefined,
        );
        expect(p.channel).toBe('lark');
        expect(p.is_p2p).toBe(false);
        expect(p.is_canary).toBe(true);
        expect(p.persona_ids).toEqual(['persona-1', 'persona-2']);
    });

    it('enricher only enriches its own channel; non-lark stays neutral', () => {
        setChatRequestEnricher((m) =>
            m.channel === 'lark'
                ? { isCanary: true, personaIds: ['x'] }
                : { isCanary: false, personaIds: [] },
        );
        const p = buildChatRequestPayload(rm({ channel: 'qq' }), 's', 'b', undefined);
        expect(p.is_canary).toBe(false);
        expect(p.persona_ids).toEqual([]);
    });
});
