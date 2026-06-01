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
// B2：is_canary / mentions 是飞书专属语义。core 的 reply.ts 不再读任何飞书
// 旁挂对象（#228 的 channelContext.larkMessage 逃生口已删）。这些飞书富化
// 字段经平台无关的「enricher 注入点」由 lark 插件提供：未注入时取中性默认
// （is_canary=false / mentions=[]），core 永远看不到飞书对象。

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

    it('no enricher registered: is_canary=false, mentions=[] (no platform binding leaked)', () => {
        const p = buildChatRequestPayload(rm({ channel: 'qq' }), 's', 'b', undefined);
        expect(p.is_canary).toBe(false);
        expect(p.mentions).toEqual([]);
    });

    it('registered enricher supplies is_canary + mentions (lark provides this seam)', () => {
        setChatRequestEnricher((m) => {
            if (m.channel !== 'lark') return { isCanary: false, mentions: [] };
            return { isCanary: true, mentions: ['app-1', 'app-2'] };
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
        expect(p.mentions).toEqual(['app-1', 'app-2']);
    });

    it('enricher only enriches its own channel; non-lark stays neutral', () => {
        setChatRequestEnricher((m) =>
            m.channel === 'lark'
                ? { isCanary: true, mentions: ['x'] }
                : { isCanary: false, mentions: [] },
        );
        const p = buildChatRequestPayload(rm({ channel: 'qq' }), 's', 'b', undefined);
        expect(p.is_canary).toBe(false);
        expect(p.mentions).toEqual([]);
    });
});
