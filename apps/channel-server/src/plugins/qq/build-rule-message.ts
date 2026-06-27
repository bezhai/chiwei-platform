// QQ InboundMessage → 平台无关 RuleMessage（对飞书 build-rule-message）。
//
// 飞书侧把富 Message put 进私有 store 供飞书谓词取回；QQ 没有平台专属谓词
// （commands=[]），文本/媒体工具直接从平台无关 ContentItem[] 派生，不需要私有
// context store。RuleMessage 保持纯平台无关视图：runRules 看到的是 isDirect、
// clearText 和 common mention ids。

import type { ContentItem, InboundMessage } from '@core/channels/contracts';
import type { RuleMessage } from '@core/rules/rule-message';

function textOf(content: ContentItem[]): string {
    return content
        .map((c) => (c.kind === 'text' || c.kind === 'unsupported' ? c.text : ''))
        .join('');
}

export function buildQqRuleMessage(
    inbound: InboundMessage,
    ids: {
        botName: string;
        commonUserId: string;
        commonConversationId: string;
        commonMessageId: string;
        commonRootMessageId: string | undefined;
        botCommonUserId: string;
        mentionedUserIds: string[];
    },
): RuleMessage {
    const content = inbound.content;
    return {
        channel: 'qq',
        botName: ids.botName,
        commonUserId: ids.commonUserId,
        commonConversationId: ids.commonConversationId,
        commonMessageId: ids.commonMessageId,
        commonRootMessageId: ids.commonRootMessageId,
        isDirect: inbound.conversation_scope === 'direct',
        botCommonUserId: ids.botCommonUserId,
        mentionedUserIds: ids.mentionedUserIds,
        createTime: inbound.received_at,
        clearText: () => textOf(content).trim(),
        text: () => textOf(content),
        withoutEmojiText: () => textOf(content),
        isTextOnly: () => content.length > 0 && content.every((c) => c.kind === 'text'),
        isStickerOnly: () => content.length === 1 && content[0].kind === 'sticker',
        stickerKey: () => {
            const sticker = content.find((c) => c.kind === 'sticker');
            return sticker && sticker.kind === 'sticker' ? sticker.key : '';
        },
        imageKeys: () =>
            content.filter((c) => c.kind === 'image').map((c) => (c as { key: string }).key),
    };
}
