/**
 * 被动回复窗口的纯决策逻辑（无 IO，便于穷举测试）。
 *
 * QQ 官方机器人只能在收到用户消息后的被动窗口内回复：
 *   - 同一原始 msg_id 维护递增的 msg_seq；
 *   - 窗口 60min；
 *   - 最多回复 4 次；
 *   - 没有 replyToMessageId 即主动发，QQ 发不出 → 直接丢弃，绝不调主动发 api；
 *   - 幂等键已出现过（MQ 重投）→ 丢弃，且不消耗 seq。
 *
 * windowStart 取「该 msg_id 第一次被动回复的时刻」。真实 QQ 窗口从用户原始消息算起，
 * 而回复通常紧随消息，两者近似；这是网关可独立维护的口径。
 */

export interface WindowRecord {
    /** 该 msg_id 第一次被动回复的时刻（ms epoch）。 */
    windowStart: number;
    /** 已为该 msg_id 预留的被动回复次数。 */
    replies: number;
}

export type DropReason = 'active_send' | 'duplicate' | 'window_expired' | 'limit_exceeded';

export type Decision =
    | { action: 'send'; msgSeq: number; nextRecord: WindowRecord }
    | { action: 'drop'; reason: DropReason };

export interface DecideInput {
    /** 出站消息是否带 replyToMessageId（被动回复）。 */
    hasReplyTo: boolean;
    /** 该幂等键此前是否已处理过。 */
    idempotencyAlreadySeen: boolean;
    /** 该 msg_id 当前窗口记录，无则 null。 */
    record: WindowRecord | null;
    now: number;
    windowMs: number;
    maxReplies: number;
}

export function decidePassiveReply(input: DecideInput): Decision {
    // 1. 主动发：QQ 官方机器人发不出，直接丢弃（绝不调主动发 api）
    if (!input.hasReplyTo) {
        return { action: 'drop', reason: 'active_send' };
    }
    // 2. 幂等去重：重投不重发，且不消耗 seq
    if (input.idempotencyAlreadySeen) {
        return { action: 'drop', reason: 'duplicate' };
    }

    const rec: WindowRecord = input.record ?? { windowStart: input.now, replies: 0 };

    // 3. 窗口过期（优先于次数判断）：窗口边界含等号内仍允许，超过即丢
    if (input.record && input.now - input.record.windowStart > input.windowMs) {
        return { action: 'drop', reason: 'window_expired' };
    }

    // 4. 次数上限
    if (rec.replies >= input.maxReplies) {
        return { action: 'drop', reason: 'limit_exceeded' };
    }

    const msgSeq = rec.replies + 1;
    return {
        action: 'send',
        msgSeq,
        nextRecord: { windowStart: rec.windowStart, replies: rec.replies + 1 },
    };
}
