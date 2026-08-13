// 安全审计判违规之后，把已经发出去的那几条飞书消息删掉。
//
// 本文件是撤回的**业务层**：它决定撤哪几条、撤成什么样算成功、台账上落什么。它不认识
// RabbitMQ —— 该 ACK 还是该退回、重投怎么发出去，都在 recall-queue.ts。所以整条撤回链
// 能在一台连不到任何后端的机器上跑完。
//
// ## 三条判定，顺序不能换
//
//   1. **终态短路**   已经是 recalled / recall_failed 就什么都不做。重复撤回必然失败
//                     （消息已经没了），第二次的结论会把第一次的 recalled 盖成
//                     recall_failed。agent-service 那侧的 TERMINAL_STATUSES 短路正是
//                     假设撤回这一侧不改写终态，这里对称做一次入口检查
//   2. **没有 replies → 重投**  行不存在、或者 replies 是空数组，都不是"没有东西要撤"，
//                     而是**出站还没落库** —— 撤回请求跑赢了发送侧的台账写入。判成
//                     无事可做的话，违规内容就永远留在群里了
//   3. **逐条撤**     单条失败不中断后续：一条删不掉不该让其余的留着不删
//
// ## 计数语义：只要有一条成功，整体就是 recalled
//
// 撤回的目的是"违规内容别再挂在群里"，删掉一条也是往那个方向走了一步，跟一条都没删掉
// 不是同一件事。failed 只进 safety_result 和日志，不改变终态的判定。
//
// 这是拆分前 channel-server recall-worker 的判定口径，照搬。

import type { CommonAgentResponseReply } from '@inner/shared/entities';

import type { LarkSpeakAs } from './deliver';
import type { LarkOutboundApi } from './lark-api';
import type { LarkResponseLedger } from './ledger';
import type { LarkOutboundTables } from './tables';

/** 重投几次之后放弃。 */
const MAX_RETRY = 3;

/**
 * 每一次重投等多久。
 *
 * 等的是出站那一侧把 replies 落库 —— 它要发一次飞书 API、写两张表、再追加台账，所以
 * 第一次就等 5 秒。表用完之后按最后一格算（实际到不了，MAX_RETRY 先拦住）。
 */
const RETRY_DELAYS = [5000, 10000, 15000];

/** 已经落地、不该再被改写的安全终态。 */
const TERMINAL_SAFETY_STATUSES = new Set(['recalled', 'recall_failed']);

/**
 * 撤回请求的消息体。**跨语言线格式**：生产者是 agent-service 的 run_post_safety。
 */
export interface LarkRecallPayload {
    /** 目标渠道。分区队列上只可能是 lark，校验在 recall-queue.ts。 */
    channel?: string;
    session_id: string;
    chat_id?: string;
    trigger_message_id?: string;
    /** 安全判定给的理由与细节。撤回这一侧原样带回台账，不解释也不改写。 */
    reason: string;
    detail?: string;
    /**
     * 上游仍然在 body 里带 lane，但**判 lane 不看这里**：lane 只认 AMQP header，
     * 口径见 @inner/shared/mq-context 的 laneFromMessage。
     */
    lane?: string;
}

/** 一次撤回处理要的全部输入。队列那一头负责从 AMQP 消息里把它拼出来。 */
export interface LarkRecallRequest {
    payload: LarkRecallPayload;
    /** 这条消息已经被重投过几次。 */
    retryCount: number;
    /** 入站 header 解析出的泳道。撤回要发生在原泳道的上下文下。 */
    lane?: string;
    /** 入站 header 上的 trace。缺省时 speakAs 会铸一条新的。 */
    traceId?: string;
}

/**
 * 处理结果。**队列那一头据此决定 ACK 还是退回**，所以这里区分的是"处置不同"的情况，
 * 不是"发生了什么"的全部细节。
 */
export type LarkRecallOutcome =
    /** 撤过了，终态已落。 */
    | { kind: 'settled'; status: 'recalled' | 'recall_failed'; recalled: number; failed: number }
    /** 之前就已经是终态，这一条什么都没做。 */
    | { kind: 'short-circuited'; status: string }
    /** replies 还没落库，延这么久之后再投一次。 */
    | { kind: 'retry'; delayMs: number; retryCount: number }
    /** 重投到顶还是没有 replies。终态已写成 recall_failed，消息该进死信。 */
    | { kind: 'exhausted' };

export interface LarkRecallDeps {
    ledger: Pick<LarkResponseLedger, 'find' | 'settleSafety'>;
    /** 公共层消息 id → 飞书裸 om_id。跟出站那条链用的是同一个反查。 */
    store: Pick<LarkOutboundTables, 'omIdOf'>;
    api: Pick<LarkOutboundApi, 'recall'>;
    speakAs: LarkSpeakAs;
    now(): number;
}

export async function recallLarkResponse(
    deps: LarkRecallDeps,
    request: LarkRecallRequest,
): Promise<LarkRecallOutcome> {
    const { payload, retryCount, lane, traceId } = request;
    const { session_id: sessionId, reason, detail } = payload;

    console.info(
        `[lark-outbound] recall: session_id=${sessionId} lane=${lane ?? 'prod'} reason=${reason}`,
    );

    const row = await deps.ledger.find(sessionId);

    if (row && TERMINAL_SAFETY_STATUSES.has(row.safety_status)) {
        console.info(
            `[lark-outbound] recall short-circuit: session_id=${sessionId} ` +
                `is already ${row.safety_status}`,
        );
        return { kind: 'short-circuited', status: row.safety_status };
    }

    if (!row || row.replies.length === 0) {
        if (retryCount < MAX_RETRY) {
            const delayMs = RETRY_DELAYS[retryCount] ?? RETRY_DELAYS[RETRY_DELAYS.length - 1]!;
            console.warn(
                `[lark-outbound] no replies yet for session_id=${sessionId}, retrying ` +
                    `(${retryCount + 1}/${MAX_RETRY}) in ${delayMs}ms`,
            );
            return { kind: 'retry', delayMs, retryCount: retryCount + 1 };
        }

        console.error(
            `[lark-outbound] recall gave up on session_id=${sessionId} after ${MAX_RETRY} ` +
                'retries; marking recall_failed and sending it to the DLQ',
        );
        // 这一笔是顺手补的：不写的话 status 永远停在 pending，台账上看不出撤回失败过。
        // 它自己失败**不该**盖掉"这条消息要进死信"这个结论，所以吞掉。
        try {
            await settleSafety(deps, sessionId, 'recall_failed', payload, {
                recalled: 0,
                failed: 0,
            });
        } catch (error) {
            console.error(
                `[lark-outbound] failed to write recall_failed for session_id=${sessionId}:`,
                error,
            );
        }
        return { kind: 'exhausted' };
    }

    // 撤回打到哪个飞书应用由上下文里的 bot 决定（见 sdk-lark-api.ts 的客户端池）。
    // 台账上没有 bot_name 时照样往下走：客户端池会拒绝猜，逐条记 failed，终态落成
    // recall_failed —— 比静默跳过强，至少台账上留得下"撤不掉"。
    let recalled = 0;
    let failed = 0;
    await deps.speakAs({ botName: row.bot_name ?? '', lane, traceId }, async () => {
        const result = await recallEach(deps, row.replies);
        recalled = result.recalled;
        failed = result.failed;
    });

    const status = recalled > 0 ? 'recalled' : 'recall_failed';
    await settleSafety(deps, sessionId, status, payload, { recalled, failed });

    if (failed > 0) {
        console.error(
            `[lark-outbound] partial recall: session_id=${sessionId} ` +
                `recalled=${recalled} failed=${failed}`,
        );
    }
    console.info(`[lark-outbound] recall completed: session_id=${sessionId} status=${status}`);
    return { kind: 'settled', status, recalled, failed };
}

/**
 * 逐条撤。**一条一条来，不并发**：撤回是删别人已经看见的东西，慢一点没关系，而并发
 * 打同一个飞书应用只会更容易撞限流。单条失败计数之后继续下一条。
 */
async function recallEach(
    deps: LarkRecallDeps,
    replies: CommonAgentResponseReply[],
): Promise<{ recalled: number; failed: number }> {
    let recalled = 0;
    let failed = 0;
    for (const reply of replies) {
        try {
            const omId = await deps.store.omIdOf(reply.common_message_id);
            if (!omId) {
                throw new Error(
                    `lark recall cannot resolve common_message_id=${reply.common_message_id}`,
                );
            }
            await deps.api.recall(omId);
            recalled += 1;
            console.info(`[lark-outbound] recalled ${reply.common_message_id} (${omId})`);
        } catch (error) {
            failed += 1;
            console.error(
                `[lark-outbound] failed to recall ${reply.common_message_id}:`,
                error,
            );
        }
    }
    return { recalled, failed };
}

/** 落安全终态。reason / detail 原样取自请求，计数与时间由本次撤回填。 */
function settleSafety(
    deps: LarkRecallDeps,
    sessionId: string,
    status: 'recalled' | 'recall_failed',
    payload: LarkRecallPayload,
    counts: { recalled: number; failed: number },
): Promise<void> {
    return deps.ledger.settleSafety(sessionId, {
        status,
        reason: payload.reason,
        detail: payload.detail,
        recalled: counts.recalled,
        failed: counts.failed,
        checkedAt: new Date(deps.now()).toISOString(),
    });
}
