// 一次回答的台账：common_agent_response 那一行。
//
// 这张表记的是"赤尾对这次提问回答成什么样了" —— 谁在答、答了哪几段、终态是什么、
// 安全判定的结论是什么。它**不属于任何渠道**（表上压根没有 channel 列），所以它是
// 拆分之后最容易被写脏的一张表：飞书的段落由 lark-service 写、QQ 的由 channel-server
// 写、人设与安全判定由 agent-service 写，三方共写同一行，DB 层拦不住越界。
// 隔离全靠消费侧的 fail-closed（见 response-queue.ts）。
//
// ## 台账写入**不在**落库事务里，这是既有形态
//
// 发出去一段 → 落 assistant 行（事务） → 追加 replies（另一条语句）。中间崩掉会
// 留下"消息发了、落库了、台账没记上"的行。保持现状不修：把台账拉进同一个事务会
// 让事务横跨一次飞书 API 调用之后的两张表，锁的持有时间被拉长，而这条链路是并发
// 消费的。
//
// ## 一个方法一件事
//
// settle 看着像"两件事"（改状态 + 可能改正文），其实是一次终态落笔：正文只在正常
// 收尾时才有，空内容收尾时**不能**把已经写好的正文抹成空。所以 outcome 里
// responseText 缺省 = 不碰这一列，而不是写 null。

import type { CommonAgentResponseReply } from '@inner/shared/entities';

/** 台账里我们需要读的那一点点：出站要前两列，撤回四列都要。 */
export interface LarkAgentResponseRow {
    session_id: string;
    /**
     * 答话的那个 bot。
     *
     * 出站消息自己也带 bot_name（agent-service 按 persona_id 反查填的），那个优先；
     * 这里是它的回退来源。两个都没有就没人能发这条消息。
     */
    bot_name?: string;
    /**
     * 已经发出去的那几段。**撤回逐条撤的就是它**。
     *
     * 空数组不等于"这次回答没有回复"，更常见的是**出站还没落库** —— 撤回请求跑赢了
     * 发送侧的台账写入。所以撤回读到空的时候走延时重投，不是判定无事可做。
     */
    replies: CommonAgentResponseReply[];
    /**
     * 安全判定的状态。撤回前要看它是不是已经落成终态（recalled / recall_failed），
     * 落了就别再撤一次 —— 重复撤回会把 recalled 覆盖成 recall_failed。
     */
    safety_status: string;
}

/** 一次回答的终态。 */
export interface LarkResponseOutcome {
    status: 'completed' | 'failed';
    /**
     * 整轮回答的全文。**缺省表示不碰 response_text 这一列**。
     *
     * 空内容收尾（agent 产出了空串）也会走到 completed，但那时没有正文可写 ——
     * 写空会把前面几段已经落好的全文抹掉。
     */
    responseText?: string;
}

/**
 * 撤回之后的安全终态。
 *
 * 跟 LarkResponseOutcome 是两回事，别合并：那个记的是"这次回答说完没有"，这个记的是
 * "说出去的话被判违规之后处理成什么样了"。两者的写入方在拆分后也不同 —— 前者只有
 * 出站，后者是 agent-service 和撤回链路双向写。
 */
export interface LarkSafetyOutcome {
    status: 'recalled' | 'recall_failed';
    /** 安全判定给的理由与细节，原样带回台账，撤回这一侧不解释也不改写。 */
    reason?: string;
    detail?: string;
    /** 逐条撤回的结果计数。 */
    recalled: number;
    failed: number;
    /** ISO 时间串。落进 safety_result 的 checked_at。 */
    checkedAt: string;
}

export interface LarkResponseLedger {
    /** 找这次回答的台账行。主动发没有台账，调用方先判 session_id 非空。 */
    find(sessionId: string): Promise<LarkAgentResponseRow | null>;

    /**
     * 往 replies 里追加一段。
     *
     * 必须是 jsonb 的 `||` 拼接而不是读-改-写：同一次回答的多段是并发消费的，
     * 读-改-写会让后写的那一段把前一段挤掉。
     */
    appendReply(sessionId: string, reply: CommonAgentResponseReply): Promise<void>;

    /** 落终态。 */
    settle(sessionId: string, outcome: LarkResponseOutcome): Promise<void>;

    /**
     * 落安全终态：safety_status + safety_result。
     *
     * 跟 settle 分开是因为写的是**另一对列、另一个写入方**：这两列 agent-service 也在
     * 写（安全判定），而表上没有 channel 列，DB 层拦不住越界。合成一个方法就等于让
     * 一次出站收尾有机会顺手覆盖掉安全判定的结论。
     */
    settleSafety(sessionId: string, outcome: LarkSafetyOutcome): Promise<void>;
}
