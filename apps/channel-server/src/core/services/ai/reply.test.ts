import { describe, it, expect } from 'bun:test';

import { buildChatRequestPayload } from './reply';
import type { RuleMessage } from 'core/rules/rule-message';

// makeTextReply 是 persona 文本主链路，决策五里唯一真正平台无关的规则。
// 它必须直接消费 RuleMessage 上已是全局 internal_*_id 的身份字段（不再绕
// channel-binding context 退回飞书裸 ID）。ChatTrigger 带 channel + 全局 ID，
// agent-service 无感知透传，ChatResponseSegment 原路带回。

function rm(over: Partial<RuleMessage> = {}): RuleMessage {
    return {
        channel: 'qq',
        botName: 'bot-q',
        internalUserId: 'GU',
        internalChatId: 'GC',
        internalMessageId: 'GM',
        internalRootId: 'GR',
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
        channelContext: undefined,
        ...over,
    };
}

describe('buildChatRequestPayload (platform-neutral persona path)', () => {
    it('uses global internal_*_id from RuleMessage, carries channel', () => {
        const p = buildChatRequestPayload(rm(), 'sess-1', 'bot-q', undefined);
        expect(p.channel).toBe('qq');
        expect(p.message_id).toBe('GM');
        expect(p.chat_id).toBe('GC');
        expect(p.user_id).toBe('GU');
        expect(p.root_id).toBe('GR');
        expect(p.is_p2p).toBe(true);
        expect(p.session_id).toBe('sess-1');
    });

    it('root_id falls back to message_id when no internalRootId', () => {
        const p = buildChatRequestPayload(
            rm({ internalRootId: undefined }),
            's',
            'b',
            undefined,
        );
        expect(p.root_id).toBe('GM');
    });

    it('lark message carries is_canary + mentions from channelContext, group scope', () => {
        const larkCtx = {
            channel: 'lark' as const,
            larkMessage: {
                basicChatInfo: { permission_config: { is_canary: true } },
                getBotAppIds: () => ['app-1', 'app-2'],
            } as never,
        };
        const p = buildChatRequestPayload(
            rm({ channel: 'lark', isDirect: false, channelContext: larkCtx }),
            's',
            'b',
            undefined,
        );
        expect(p.channel).toBe('lark');
        expect(p.is_p2p).toBe(false);
        expect(p.is_canary).toBe(true);
        expect(p.mentions).toEqual(['app-1', 'app-2']);
    });

    it('non-lark message: is_canary=false, mentions=[] (no lark binding leaked)', () => {
        const p = buildChatRequestPayload(rm({ channel: 'qq' }), 's', 'b', undefined);
        expect(p.is_canary).toBe(false);
        expect(p.mentions).toEqual([]);
    });
});
