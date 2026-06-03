// 飞书寻址策略：判断一条入站消息是否冲 bot 来、要不要响应。实现 contracts.ts
// 的 AddressingPolicy 契约（decide），与现状 NeedRobotMention 逻辑等价：
//   NeedRobotMention = mentionedUserIds.includes(botCommonUserId) || message.isP2P()
// 其中 isP2P() <=> conversation_scope === 'direct'（inbound 把 p2p 映射到
// direct）。这里是 Lark 插件自己的前置总闸，仍用飞书 union_id 同口径比较；
// 进入 runRules 前，common-projector 会把 mention list 换成 common_user_id。
//
// 决策刻意带非空 reason：不响应也必须说清"为什么不回"，让 enforceDecision 在
// 边界把静默丢弃炸出来。

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
        return {
            respond: true,
            reason: `bot ${botMentionTarget} mentioned in group conversation`,
        };
    }
    return {
        respond: false,
        reason: `group message without bot ${botMentionTarget} mention; not addressed to bot`,
    };
}

export const larkAddressing: AddressingPolicy = {
    decide,
};
