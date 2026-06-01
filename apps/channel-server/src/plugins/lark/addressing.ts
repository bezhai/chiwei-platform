// 飞书寻址策略：判断一条入站消息是否冲 bot 来、要不要响应。实现 contracts.ts
// 的 AddressingPolicy 契约（decide），与现状 NeedRobotMention 逻辑等价：
//   NeedRobotMention = message.hasMention(getBotUnionId()) || message.isP2P()
// 其中 isP2P() <=> conversation_scope === 'direct'（inbound 把 p2p 映射到
// direct）；hasMention(botUnionId) <=> addressing_hints 里有 targetId 等于
// botMentionTarget（addressing_hints 由 inbound 从 mentions[].id.union_id 产出，与
// botMentionTarget=robot_union_id 同口径）。botMentionTarget 由调用方按 channel 取（飞书
// 是 robot_union_id）传入，本策略不自己读 context，保持解耦。
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
