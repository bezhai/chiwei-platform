// QQ 寻址策略（AddressingPolicy 契约）。
//   - 私聊：总响应（与飞书 p2p 等价）。
//   - 群聊：仅当消息 @ 了本 bot（网关给的 mention.isSelf=true，入站折成
//     QQ_SELF_MENTION_TARGET 哨兵）才响应。
// 不响应必须带非空 reason，让 enforceDecision 在边界把静默丢弃炸出来。

import type {
    AddressingDecision,
    AddressingPolicy,
    InboundMessage,
} from '@core/channels/contracts';

function decide(msg: InboundMessage, botMentionTarget: string): AddressingDecision {
    if (msg.conversation_scope === 'direct') {
        return { respond: true, reason: 'direct conversation: bot always responds' };
    }
    const mentioned = msg.addressing_hints.some((h) => h.targetId === botMentionTarget);
    if (mentioned) {
        return { respond: true, reason: 'bot @mentioned (isSelf) in group conversation' };
    }
    return {
        respond: false,
        reason: 'group message without bot @mention (isSelf); not addressed to bot',
    };
}

export const qqAddressing: AddressingPolicy = {
    decide,
};
