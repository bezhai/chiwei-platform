// 把已经发出去的那几条飞书消息删掉。
//
// 本文件是撤回的**业务层**：它决定撤哪几条、撤成什么样算成功、库里落什么。它不认识
// RabbitMQ —— 该 ACK 还是该退回、重投怎么发出去，都在 recall-queue.ts。所以整条撤回链
// 能在一台连不到任何后端的机器上跑完。
//
// ## 两种定位方式，恰好用一种
//
// 跨语言线格式，共享向量 contracts/recall-locators.json：
//
//   session_id    真人问她、她答的那条链。按会话标识查台账（common_agent_response）
//                 拿到那次落下的全部回复，逐条撤
//   outbound_id   她自己开口那条链。她没有会话，只有一次开口；按这个 id 等值反查公共层
//                 那几行（common_message.agent_outbound_id），**不碰台账**
//
// 主动消息在台账上一行都没有：投递方对那张表只 UPDATE 不 INSERT，而主动发没有会话标识，
// 一路被守卫跳过。所以拿一个假的会话标识来撤主动消息，后果是静默的 —— 查台账、查不到、
// 退避重投三次、写一行影响 0 行的失败、进死信，一个飞书接口都不会调，消息安安静静留在
// 群里，全程不抛一个异常。两种定位方式的分派因此在最前面做一次，"不是恰好一个"直接
// 判成坏请求。
//
// ## 会话那条链的三条判定，顺序不能换
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
//
// ## 「删掉了」和「记下来了」是两件事
//
// 主动开口那条链上，撤成什么样唯一落地的地方是 common_message.recalled_at，而上游
// （她手机上那份）就是按这一列判"这句话还在不在"的。所以删掉了却没写上，库里就是
// **假的** —— 她会看见一条实际上已经不存在的消息，还可能基于它说话。
//
// 于是那一段既不算 recalled 也不算 failed，单独计 unrecorded，整条走退避重投，到顶
// 交给死信。**重投在这条链上是安全的**：撤回不会让真人再收到任何东西（出站那条
// "飞书接口调过之后不许让上游重投"的不变量是针对发送的，理由正是真人会收到第二条）。
//
// 但安全的只是"真人看不到"，**不是"第二次删会成功"**：飞书对一条已经删掉的消息返回
// 非 0 code（见 lark-api.ts 的 recall）。重投靠两件事收敛：
//
//   1. 先读 recalled_at。上一次其实写成功了的那些直接认，不再删一遍
//   2. 真的又删了一次、飞书回 99991663（消息已被撤回或删除）时**认成已撤**，照样写
//      recalled_at。这一条堵的正是 1 堵不住的那个缺口：上一次删成功而 recalled_at
//      没写上，重投回来那一列还是 NULL，短路不生效，第二次删必然被飞书拒绝 ——
//      不认这个码的话，那一列就永远写不上（判定见 recallEachProactive）
//
// 飞书那个数字码经 @inner/lark-utils 挂在抛出的 Error 上（larkErrorCode 读），业务层
// 据此自己判"这一种失败算不算失败"。

import { LARK_MESSAGE_ALREADY_RECALLED, larkErrorCode } from '@inner/lark-utils';
import type { CommonAgentResponseReply } from '@inner/shared/entities';

import type { LarkSpeakAs } from './deliver';
import type { LarkOutboundApi } from './lark-api';
import type { LarkResponseLedger } from './ledger';
import type { LarkProactiveMessageRow, LarkRecallTables } from './tables';

/** 重投几次之后放弃。 */
const MAX_RETRY = 3;

/**
 * 每一次重投等多久。
 *
 * 等的是投递方把这次说的话落库 —— 它要发一次飞书 API、写两张表，被动回复还要再追加
 * 一次台账，所以第一次就等 5 秒。表用完之后按最后一格算（实际到不了，MAX_RETRY
 * 先拦住）。
 */
const RETRY_DELAYS = [5000, 10000, 15000];

/** 已经落地、不该再被改写的安全终态。 */
const TERMINAL_SAFETY_STATUSES = new Set(['recalled', 'recall_failed']);

/**
 * 撤回请求的消息体。**跨语言线格式**：生产者是 agent-service 的 Recall。
 */
export interface LarkRecallPayload {
    /** 目标渠道。分区队列上只可能是 lark，校验在 recall-queue.ts。 */
    channel?: string;
    /**
     * 两种定位方式，**恰好用一种**，形状见 contracts/recall-locators.json。
     *
     * 两个都写成必填（而不是 `?`），是因为漏写和"这条链不用它"在这里长得一模一样：
     * 一个 undefined 的 session_id 会被下面的分派当成"那就走主动开口那条"，而
     * 一个 undefined 的 outbound_id 会让主动消息掉进查台账那条路 —— 那条路对主动
     * 消息的失败方式是静默的（见文件头）。写成必填之后，任何一处构造撤回请求的代码
     * 都得显式说出这一条撤的是哪一种，不能默默继承一个空值。
     *
     * 生产者那一侧同样两个都给（Pydantic 的字段默认值是 None，两个键都会进 payload），
     * 并且在构造时就判了"恰好一个"。
     */
    session_id: string | null;
    outbound_id: string | null;
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
    /** 撤过了，结论已落。 */
    | { kind: 'settled'; status: 'recalled' | 'recall_failed'; recalled: number; failed: number }
    /** 之前就已经是终态，这一条什么都没做。 */
    | { kind: 'short-circuited'; status: string }
    /** 要撤的那几条还没落库，延这么久之后再投一次。 */
    | { kind: 'retry'; delayMs: number; retryCount: number }
    /**
     * 放弃这一条，交给死信留个痕。两种情况：重投到顶还是查不到要撤的消息（会话那条
     * 链此时已经把终态写成 recall_failed），以及请求本身指不到任何一条消息。
     */
    | { kind: 'exhausted' };

export interface LarkRecallDeps {
    ledger: Pick<LarkResponseLedger, 'find' | 'settleSafety'>;
    /**
     * 公共层那几条读写：om_id 反查跟出站那条链用的是同一个，另外两条只有撤主动消息
     * 用得上（见 tables.ts 的 LarkRecallTables）。
     */
    store: LarkRecallTables;
    api: Pick<LarkOutboundApi, 'recall'>;
    speakAs: LarkSpeakAs;
    now(): number;
}

/**
 * 32 个十六进制字符：outbound_id 在线上的写法（uuid 的 hex），契约见
 * contracts/recall-locators.json。
 */
const OUTBOUND_ID_HEX = /^[0-9a-f]{32}$/i;

/**
 * outbound_id 的 hex 写法 → 标准 uuid 文本。形状不对返回 undefined。
 *
 * PG 两种写法都收（实测 17.2：`'55f3469bd46c5384a9ce22cb4944b77a'::uuid` 解析成带短横
 * 的那个值，拿它跟 uuid 列比较照样走 agent_outbound_id 上那个索引），所以换写法不是
 * 为了让查询能跑，理由是另外两条：
 *
 *   1. 落库那一侧写进这一列的就是带短横的写法（deliver.ts 从 `proactive:<uuid>` 里剥
 *      出来的），撤回这一侧用同一个字符串，日志、参数和库里的值能逐字对上 —— 下面
 *      认合成假键也要用它
 *   2. 形状不对的输入在进 SQL 之前就挡住。塞进去的话 PG 抛 invalid input syntax，
 *      那个异常从消费者 handler 里穿出去，这条消息的处置就不再由本文件决定
 */
function uuidOfOutboundId(outboundId: string): string | undefined {
    if (!OUTBOUND_ID_HEX.test(outboundId)) return undefined;
    const hex = outboundId.toLowerCase();
    return [
        hex.slice(0, 8),
        hex.slice(8, 12),
        hex.slice(12, 16),
        hex.slice(16, 20),
        hex.slice(20),
    ].join('-');
}

/**
 * 要撤的那几条还查不到时的退避决定。到顶返回 null。
 *
 * 两条链共用一张退避表：等的是同一件事 —— 投递方把这次说的话写进库。
 */
function backOff(retryCount: number): { kind: 'retry'; delayMs: number; retryCount: number } | null {
    if (retryCount >= MAX_RETRY) return null;
    const delayMs = RETRY_DELAYS[retryCount] ?? RETRY_DELAYS[RETRY_DELAYS.length - 1]!;
    return { kind: 'retry', delayMs, retryCount: retryCount + 1 };
}

export async function recallLarkResponse(
    deps: LarkRecallDeps,
    request: LarkRecallRequest,
): Promise<LarkRecallOutcome> {
    const { session_id: sessionId, outbound_id: outboundId } = request.payload;

    // 恰好一种定位方式。两个都给或者一个都没给都是坏请求：猜一个的代价是撤错东西，
    // 或者拿空串去查台账 —— 后者的失败方式是静默的（见文件头）。
    if ([sessionId, outboundId].filter(Boolean).length !== 1) {
        console.error(
            `[lark-outbound] recall must point at exactly one thing, got ` +
                `session_id=${sessionId} outbound_id=${outboundId}; sending it to the DLQ`,
        );
        return { kind: 'exhausted' };
    }

    if (outboundId) return recallProactiveMessage(deps, request, outboundId);
    return recallSessionReplies(deps, request, sessionId!);
}

/**
 * 她自己开口那条：按 outbound_id 反查公共层那几行，逐段撤，撤掉的记下撤回时刻。
 *
 * **不碰台账**：主动消息在 common_agent_response 上一行都没有。撤成什么样，唯一落地
 * 的地方是 common_message.recalled_at，而它只在真的删掉之后才写 —— 撤不掉却写上，
 * 上游会据此告诉她"收回去了"。
 */
async function recallProactiveMessage(
    deps: LarkRecallDeps,
    request: LarkRecallRequest,
    outboundId: string,
): Promise<LarkRecallOutcome> {
    const { retryCount, lane, traceId } = request;
    const { reason } = request.payload;

    const agentOutboundId = uuidOfOutboundId(outboundId);
    if (!agentOutboundId) {
        console.error(
            `[lark-outbound] recall outbound_id is not a uuid hex: "${outboundId}"; ` +
                'nothing can be looked up with it, sending it to the DLQ',
        );
        return { kind: 'exhausted' };
    }

    console.info(
        `[lark-outbound] recall: outbound_id=${outboundId} lane=${lane ?? 'prod'} reason=${reason}`,
    );

    const rows = await deps.store.messagesOfAgentOutbound(agentOutboundId);

    // 一行都没有**不是**"没有东西要撤"：她可能刚说完就想撤，这一刻投递方还没把这条
    // 消息写进公共层。判成无事可做的话，那句话就永远留在群里了。
    if (rows.length === 0) {
        const again = backOff(retryCount);
        if (again) {
            console.warn(
                `[lark-outbound] outbound_id=${outboundId} has not landed yet, retrying ` +
                    `(${again.retryCount}/${MAX_RETRY}) in ${again.delayMs}ms`,
            );
            return again;
        }
        // 会话那条链此时会往台账上补一笔 recall_failed；这条链没有台账行，能留下的
        // 痕迹只有死信和这行日志。
        console.error(
            `[lark-outbound] recall gave up on outbound_id=${outboundId} after ${MAX_RETRY} ` +
                'retries; it never landed in the common layer, sending it to the DLQ',
        );
        return { kind: 'exhausted' };
    }

    // 飞书只让发送者撤自己的消息，所以身份必须是当时发这条消息的那个 bot —— 主动发
    // 没有台账行，它只能从消息行上拿（出站落库时写的就是它）。行上没有时照样往下走：
    // 客户端池会拒绝猜，逐条记 failed。
    let recalled = 0;
    let failed = 0;
    let unrecorded = 0;
    await deps.speakAs({ botName: rows[0]!.bot_name ?? '', lane, traceId }, async () => {
        const result = await recallEachProactive(deps, rows, agentOutboundId);
        recalled = result.recalled;
        failed = result.failed;
        unrecorded = result.unrecorded;
    });

    // 删掉了、却没记下撤回时刻：飞书上那条消息已经不在，公共层还说它在。上游（她手机
    // 上那份）就是按 recalled_at 判"这句话还在不在"的，所以此刻库里是假的 —— 她会看见
    // 一条实际上不存在的消息，还可能基于它说话。
    //
    // 判成 settled 就等于 ACK，这条消息从队列上消失，再也没有第二次机会。所以走退避
    // 重投，到顶交给死信 —— 死信里那条能查、能重放，一行日志不能。
    //
    // **这里往回退跟出站那条不变量不冲突。** 出站那条是"飞书接口调过之后不许让上游
    // 重投"，理由是重投会让真人再收到一条消息。撤回这条链上重投最多是再打一次删除
    // 调用，真人一个字都看不到，所以重投是安全的。注意安全的是"真人看不到"，**不是**
    // "第二次删会成功" —— 飞书对一条已经删掉的消息返回非 0 code（见 lark-api.ts 的
    // recall）。重投能收敛靠两件事：上面那一列的短路（上一次写成功了的直接认），
    // 以及 recallEachProactive 里对 99991663 的判定（真的又删了一次、飞书说它已经
    // 不在了，照样写 recalled_at）。后者才是这个分支能走出去的那一条。
    if (unrecorded > 0) {
        const again = backOff(retryCount);
        if (again) {
            console.warn(
                `[lark-outbound] outbound_id=${outboundId} deleted ${unrecorded} message(s) ` +
                    `at lark but could not write recalled_at; retrying ` +
                    `(${again.retryCount}/${MAX_RETRY}) in ${again.delayMs}ms`,
            );
            return again;
        }
        console.error(
            `[lark-outbound] recall gave up on outbound_id=${outboundId} after ${MAX_RETRY} ` +
                `retries; ${unrecorded} message(s) are gone from lark but still read as ` +
                'never recalled, sending it to the DLQ',
        );
        return { kind: 'exhausted' };
    }

    const status = recalled > 0 ? 'recalled' : 'recall_failed';
    if (failed > 0) {
        console.error(
            `[lark-outbound] partial recall: outbound_id=${outboundId} ` +
                `recalled=${recalled} failed=${failed}`,
        );
    }
    console.info(`[lark-outbound] recall completed: outbound_id=${outboundId} status=${status}`);
    return { kind: 'settled', status, recalled, failed };
}

/**
 * 逐段撤她那次开口发出去的消息。串行、单段失败不中断后续，理由同 recallEach。
 *
 * 撤掉一段就写一段的 recalled_at，不攒到最后一起写：中间崩掉的话，已经删掉的那几段
 * 至少在库里是"撤掉了"。
 *
 * 三种结果，处置各不相同，所以分三个计数：
 *
 *   recalled    这一段已经不在飞书上了，而且库里也这么写着 —— 真的撤完了
 *   failed      这一段还在飞书上（删不掉、或者压根定位不到）—— 结论确定，是"撤不掉"
 *   unrecorded  这一段已经不在飞书上了，但撤回时刻没写下去 —— 库里此刻是**假的**，
 *               调用方据此让整条重投
 */
async function recallEachProactive(
    deps: LarkRecallDeps,
    rows: LarkProactiveMessageRow[],
    agentOutboundId: string,
): Promise<{ recalled: number; failed: number; unrecorded: number }> {
    let recalled = 0;
    let failed = 0;
    let unrecorded = 0;
    for (const row of rows) {
        // 这一段上一次已经删掉、而且记下来了。主动消息在台账上一行都没有，这一列就是
        // 它唯一的终态记录 —— 会话那条链上由 safety_status 承担的短路，在这条链上由它
        // 承担。省掉的是一次白打的删除调用：飞书对一条已经删掉的消息返回非 0 code
        // （见 lark-api.ts 的 recall），结论下面认得出来，但没必要真去问一趟。
        if (row.recalled_at) {
            recalled += 1;
            console.info(
                `[lark-outbound] ${row.common_message_id} was already recalled at ` +
                    `${row.recalled_at.toISOString()}; not deleting it a second time`,
            );
            continue;
        }
        try {
            const omId = await deps.store.omIdOf(row.common_message_id);
            if (!omId) {
                throw new Error(
                    `lark recall cannot resolve common_message_id=${row.common_message_id}`,
                );
            }
            if (omId.startsWith(`${agentOutboundId}_part`)) {
                // 飞书当时返回成功却没给消息标识，落库的是一个合成的键（见 deliver.ts
                // 的 record）。它不是飞书的消息标识，拿它去调删除接口必定失败 ——
                // 认出来只是省掉那一次白打的调用，结论一样是撤不掉。
                throw new Error(
                    `lark never returned a message id for ${row.common_message_id}; ` +
                        `"${omId}" is a synthesised key and cannot be deleted`,
                );
            }
            try {
                await deps.api.recall(omId);
                console.info(`[lark-outbound] recalled ${row.common_message_id} (${omId})`);
            } catch (error) {
                // 飞书说这条消息已经被撤回或删除。**这不是撤不掉，是已经撤掉了。**
                //
                // 她撤的是**她自己发出去的**消息，而飞书只允许发送者撤回自己的消息，
                // 所以这条不可能是被别人撤掉的。收到这个码只有两种来源：上一次撤成功了
                // （下面写 recalled_at 那步没落上），或者这一次之前它已经被撤过。两种
                // 都意味着**真人现在看不到它**，那正是 recalled_at 要记的事实 —— 所以
                // 往下走去写那一列，而不是记成撤不掉。
                //
                // 这一条正是让重投能收敛的原因：删成功、写库失败之后重投回来，
                // recalled_at 还是 NULL，短路不生效，于是再删一次并落到这里。没有它的
                // 话第二次删必然记成 failed 并 ACK，那一列永远写不上。
                //
                // **只认这一个码。** 超出飞书撤回时限之类的其它非 0 码是真的撤不掉，
                // 消息还挂在群里；一并当成功就是把"撤不掉"记成"撤掉了"，她据此以为那
                // 句话没了。所以别的错误原样往外抛，由下面那个 catch 记 failed。
                if (larkErrorCode(error) !== LARK_MESSAGE_ALREADY_RECALLED) throw error;
                console.info(
                    `[lark-outbound] lark says ${row.common_message_id} (${omId}) is already ` +
                        'recalled or deleted; recording the recall instead of counting a failure',
                );
            }

            // **这一段之后不再算 failed。** 消息已经没了，剩下的只是记不记得下来 ——
            // 那是另一件事，混进 failed 会让终态说成"撤不掉"，而它明明撤掉了。
            try {
                await deps.store.markRecalled(row.common_message_id, new Date(deps.now()));
                recalled += 1;
            } catch (error) {
                unrecorded += 1;
                console.error(
                    `[lark-outbound] recalled ${row.common_message_id} (${omId}) but could ` +
                        'not write recalled_at; it would read as never recalled:',
                    error,
                );
            }
        } catch (error) {
            failed += 1;
            console.error(`[lark-outbound] failed to recall ${row.common_message_id}:`, error);
        }
    }
    return { recalled, failed, unrecorded };
}

/** 真人问她、她答的那条：按会话标识查台账，撤那次落下的全部回复。 */
async function recallSessionReplies(
    deps: LarkRecallDeps,
    request: LarkRecallRequest,
    sessionId: string,
): Promise<LarkRecallOutcome> {
    const { payload, retryCount, lane, traceId } = request;
    const { reason } = payload;

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
        const again = backOff(retryCount);
        if (again) {
            console.warn(
                `[lark-outbound] no replies yet for session_id=${sessionId}, retrying ` +
                    `(${again.retryCount}/${MAX_RETRY}) in ${again.delayMs}ms`,
            );
            return again;
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
