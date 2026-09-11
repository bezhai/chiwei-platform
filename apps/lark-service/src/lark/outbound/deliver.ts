// 把赤尾说的这一段送到飞书：反查 → 渲染 → 发送 → 落库 → 记台账。
//
// 本文件是出站的**业务层**：它决定发给谁、用什么方式发、发完记什么。它不认识
// RabbitMQ、不认识 TypeORM、不认识飞书 SDK —— 那些在 response-queue.ts、
// postgres-*.ts、sdk-lark-api.ts 里，各自一层。所以整条链能在一台连不到任何后端
// 的机器上跑完。
//
// ## 顺序是不变量
//
//   1. 反查   公共层 id → 飞书坐标。查不到就**抛**，绝不静默发到别的会话去
//   2. 渲染   markdown → 飞书富文本（mention 先于图片，见 render.ts）
//   3. 发送   part 0 回复触发消息 / 主动发与续段新发
//   4. 落库   assistant 行 ＋ 飞书映射，**同一个事务**
//   5. 台账   replies 追加 ＋ 收尾时的终态，**不在上面那个事务里**
//
// 4 和 5 之间没有事务保护是既有形态：把台账拉进同一个事务，会让事务横跨一次飞书
// API 之后的两张表，锁的持有时间被拉长，而这条链路是并发消费的。
//
// ## 失败往不往外抛，分界线是「这一刻飞书 API 调没调过」
//
// **调过了**（包括调用本身抛错 —— 请求可能已经落到对面，只是响应没回来）：一律
// 吃掉，只在台账上记 failed，上游因此 ACK。重投会让"已经发出去一半的分段消息"
// 再发一遍，真人看到两条；代价是发不出去的消息就真的没了，只剩一行错误日志。
//
// **还没调过**（台账那一次读、agent 自己报的失败、空的收尾段、反查失败）：一律
// 往外抛，交给队列层处置。此时消息一个字都没发出去，重投是安全的，而吃掉台账写
// 失败等于把一次数据库抖动变成一条**永远停在 pending** 的回复 —— 它本来救得回来。
//
// 这条线跟"在不在 try 块里"不是一回事：反查失败落在 catch 里，但它发生在发送之前。
// 所以分界线由一个显式的标志表达（spokeToLark），不由代码结构隐含。
//
// ## 已知残留：ACK 之前崩溃会重发
//
// 飞书 API 已经调完、ACK 还没发出去的那一瞬间进程被杀，消息会 requeue，换个消费者
// 再发一次，真人看到两条。这个窗口拆分前就存在（chat-response-handler 发送后无条件
// ACK），拆分只是把它从"进程偶然崩溃"扩大到"我们主动触发交接"。修它要引入发送级
// 幂等（从稳定键派生确定性 common_message_id + 发送前查重 + 强制该 id 落库），
// 跨服务、动共享写路径，不在这一批里。

import type { LarkChatResponse } from './chat-response';
import type { LarkOutboundApi } from './lark-api';
import type { LarkResponseLedger } from './ledger';
import type { LarkPostRenderer } from './render';
import type { LarkOutboundStore } from './tables';

/**
 * 续段之间的固定间隔。
 *
 * 一次回答被切成几段之后，如果不停顿地连发，飞书那边会把它们挤成一坨，读起来像
 * 刷屏。这是出站的节奏，不是渲染 —— 所以它在这一层，不在 render.ts 里。
 */
const SEGMENT_GAP_MS = 2_500;

/** 出站消息的类型。只发富文本。 */
const OUTBOUND_MESSAGE_TYPE = 'post';

/**
 * 主动发的伪 message_id 前缀。
 *
 * 线格式那一侧由 agent-service 的 `PROACTIVE_MESSAGE_ID_PREFIX`
 * （app/domain/chat_dataflow.py）产出。跨语言没法共享一个运行时定义（这个服务是
 * `bun build --compile` 出来的独立二进制，镜像里只有二进制、没有仓库），所以线格式
 * 落在一份两侧测试共读的向量上：`contracts/proactive-message-id.json`。
 *
 * 改这个字面量而不改那份向量，本文件的 proactive-message-id.test.ts 立刻转红 ——
 * 没有这道闸的话，只改一边的症状是主动发的 agent_outbound_id 静默变空、那次开口
 * 在库里永久失联，全程零报错。
 */
export const PROACTIVE_MESSAGE_ID_PREFIX = 'proactive:';

/** 标准 uuid 的形状。版本位不校验：pg 的 uuid 列本来就只认这个排布。 */
const UUID_SHAPE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/**
 * 从主动发的伪 message_id 里取出「这是哪一次开口」那个 id。
 *
 * `proactive:<uuid>` → `<uuid>`（小写）。**列是 uuid 类型，带前缀的整串进不去。**
 *
 * 形状不对就返回 undefined，**绝不抛**：这个函数跑在飞书 API 已经调过之后，抛错
 * 会让上游重投、真人收到第二条（见文件头「分界线」）。记不下"是哪次开口"的代价
 * 是这一行退化成加列之前的样子，比重复打扰真人轻得多。
 */
function agentOutboundIdOf(messageId: string): string | undefined {
    if (!messageId.startsWith(PROACTIVE_MESSAGE_ID_PREFIX)) return undefined;
    const derived = messageId.slice(PROACTIVE_MESSAGE_ID_PREFIX.length);
    if (!UUID_SHAPE.test(derived)) return undefined;
    return derived.toLowerCase();
}

/** 观测点。名字与拆分前 chat-response-worker 的 stage 标签逐字一致。 */
export type LarkDeliveryStage = 'db_query' | 'channel_send' | 'db_write' | 'total';

/**
 * 在"这个 bot 正在说话"的上下文里跑一段。
 *
 * 飞书客户端按 bot 分池，选哪个客户端由上下文决定（见 sdk-lark-api.ts）——
 * 出站是并发消费，两条消息交错跑，用进程级的"当前 bot"会互相改掉对方的值、
 * 从错的人设发出去。所以它必须是一个真正的作用域，而不是一次赋值。
 */
export type LarkSpeakAs = (
    who: {
        botName: string;
        lane?: string;
        /**
         * 这一段处理属于哪条 trace。**缺省表示铸一条新的。**
         *
         * 发消息这条链故意不传（照搬拆分前 chat-response-worker 的行为，见
         * bot-context.ts）；撤回那条链传入站 header 上的那个，因为它自己还会往外发
         * 一条延时重投，重投的 trace 取自上下文 —— 不接上就是每次重试换一条 trace，
         * 而重试路径恰恰是最需要追的。
         */
        traceId?: string;
    },
    say: () => Promise<void>,
) => Promise<void>;

export interface LarkDeliveryDeps {
    store: LarkOutboundStore;
    ledger: LarkResponseLedger;
    /**
     * 发消息用的那两个方法，不是整个端口。
     *
     * 端口本身覆盖的是本服务对飞书的全部动作（指令要回卡片、定时任务要发日报、卡片
     * 回调要打裸端点），而这条链只发富文本。声明成整个端口的后果不是权限问题，是
     * 读的人无从知道这条链到底会打飞书的哪些接口。撤回那条链已经是这个写法。
     */
    api: Pick<LarkOutboundApi, 'sendPost' | 'replyPost'>;
    render: LarkPostRenderer;
    /** 这个 bot 在 common_user 里的身份。取不到**抛** —— 写空串等于写脏数据。 */
    botCommonUserId(botName: string): string;
    /** 出站消息上署的名（人设名）。没绑人设就没有。 */
    botDisplayName(botName: string): string | undefined;
    /** 铸一个新的公共层消息 id。 */
    newCommonId(): string;
    now(): number;
    wait(ms: number): Promise<void>;
    speakAs: LarkSpeakAs;
    observe(stage: LarkDeliveryStage, seconds: number): void;
}

/** 反查出来的飞书坐标。 */
interface LarkTarget {
    /** 会话。 */
    chatId: string;
    /** 触发这次回复的那条飞书消息。主动发没有来源消息，是空串。 */
    omId: string;
}

export async function deliverLarkChatResponse(
    deps: LarkDeliveryDeps,
    response: LarkChatResponse,
    lane?: string,
): Promise<void> {
    const startedAt = Date.now();
    const sessionId = response.session_id;
    const partIndex = response.part_index ?? 0;
    const isLast = response.is_last ?? false;
    const isProactive = response.is_proactive ?? false;

    // ---- 台账那一次读：在任何副作用之前，所以失败可以安全地往外抛 ----
    // 主动发没有台账行（session_id 为 null），拿 null 去 findOneBy 会误匹配，
    // 直接跳过 —— bot_name 由 payload 给。
    const readStartedAt = Date.now();
    const ledgerRow = sessionId ? await deps.ledger.find(sessionId) : null;
    deps.observe('db_query', (Date.now() - readStartedAt) / 1000);

    const botName = response.bot_name || ledgerRow?.bot_name;
    if (!botName) {
        // 没人能认领这条消息。猜一个 bot 的后果是用户看见另一个人设开口说话。
        console.error(
            `[lark-outbound] no bot to speak for session=${sessionId} ` +
                `proactive=${isProactive}; dropping the segment`,
        );
        return;
    }

    await deps.speakAs({ botName, lane }, async () => {
        if (response.status === 'failed') {
            console.error(
                `[lark-outbound] agent failed: session=${sessionId} error=${response.error}`,
            );
            // 消息一个字都没发出去，所以这次写入**失败就抛**：重投安全，而吞掉会让
            // 这一行台账永远停在 pending（见文件头「分界线」）。
            if (ledgerRow) await deps.ledger.settle(sessionId!, { status: 'failed' });
            return;
        }

        if (!response.content) {
            console.warn(`[lark-outbound] empty content: session=${sessionId} part=${partIndex}`);
            // 收尾那一段是空的仍然要收口，否则这次回答永远停在 pending。
            // **不带 responseText** —— 写空会把前面几段落好的全文抹掉。
            // 同样在发送之前，同样失败就抛。
            if (isLast && ledgerRow) {
                await deps.ledger.settle(sessionId!, { status: 'completed' });
            }
            return;
        }

        // 分界线本身。飞书 API 抛错**不代表**请求没送到对面，所以它在调用之前置位。
        let spokeToLark = false;
        try {
            const target = await resolveTarget(deps, response, isProactive);

            // 续段之间停一下。放在发送之前，放在反查之后 —— 反查失败的话根本不用等。
            if (partIndex > 0) await deps.wait(SEGMENT_GAP_MS);

            const sendStartedAt = Date.now();
            spokeToLark = true;
            const sentMessageId = await send(deps, response, target, partIndex, isProactive);
            const sendSeconds = (Date.now() - sendStartedAt) / 1000;
            deps.observe('channel_send', sendSeconds);

            const writeStartedAt = Date.now();
            const commonMessageId = await record(
                deps,
                response,
                target,
                sentMessageId,
                botName,
                partIndex,
                isProactive,
            );

            if (ledgerRow) {
                await deps.ledger.appendReply(sessionId!, {
                    common_message_id: commonMessageId,
                    content_type: OUTBOUND_MESSAGE_TYPE,
                    sent_at: new Date(deps.now()).toISOString(),
                });
                if (isLast) {
                    await deps.ledger.settle(sessionId!, {
                        status: 'completed',
                        responseText: response.full_content || response.content,
                    });
                }
            }
            const writeSeconds = (Date.now() - writeStartedAt) / 1000;
            deps.observe('db_write', writeSeconds);

            const totalSeconds = (Date.now() - startedAt) / 1000;
            deps.observe('total', totalSeconds);
            // 稳定的 event 名，make logs KEYWORD=chat_response_done 可捞。切流时
            // "新路径全量、旧路径零流量"就是数这一行。
            console.info(
                JSON.stringify({
                    event: 'chat_response_done',
                    channel: 'lark',
                    session_id: sessionId,
                    part_index: partIndex,
                    is_last: isLast,
                    is_proactive: isProactive,
                    common_message_id: commonMessageId,
                    send_ms: Math.round(sendSeconds * 1000),
                    db_write_ms: Math.round(writeSeconds * 1000),
                    total_ms: Math.round(totalSeconds * 1000),
                }),
            );
        } catch (error) {
            // 带够排查字段，别只留一句 stack：主动发没有 session_id，只能靠
            // chat_id / persona_id 定位是哪条发不出去。
            console.error(
                JSON.stringify({
                    event: 'chat_response_outbound_failed',
                    channel: 'lark',
                    session_id: sessionId,
                    chat_id: response.chat_id,
                    bot_name: botName,
                    persona_id: response.persona_id ?? null,
                    part_index: partIndex,
                    is_proactive: isProactive,
                    error: error instanceof Error ? error.message : String(error),
                }),
                error,
            );
            if (ledgerRow) await settleFailed(deps, sessionId!, spokeToLark);
        }
    });
}

/**
 * 公共层 id → 飞书坐标。
 *
 * 两种出站的反查方式不同：
 *
 *   被动回复  查来源消息 ＋ 会话 ＋（有 root 时）root，回复的锚点是来源消息
 *   主动发    **只查会话**。message_id 是伪 id（`proactive:<uuid5>`），反查必 miss；
 *             而且 root_id 被**刻意忽略** —— 主动发是赤尾自己开口，本就该是一条新
 *             消息。root_id 偶然带了值也不该让它退化成一条回复。
 *
 * 查不到一律抛。静默发到错的会话，比发不出去严重得多。
 */
async function resolveTarget(
    deps: LarkDeliveryDeps,
    response: LarkChatResponse,
    isProactive: boolean,
): Promise<LarkTarget> {
    if (isProactive) {
        return { chatId: await chatIdOrThrow(deps, response.chat_id), omId: '' };
    }

    // 顺序照搬拆分前：先来源消息、再会话、最后 root。查不到时先炸哪一个是有意义的
    // ——报错文案指向的就是第一个断掉的映射。
    const omId = await omIdOrThrow(deps, response.message_id);
    const chatId = await chatIdOrThrow(deps, response.chat_id);
    if (response.root_id) {
        await omIdOrThrow(deps, response.root_id, 'root ');
    }
    return { chatId, omId };
}

async function chatIdOrThrow(deps: LarkDeliveryDeps, commonConversationId: string): Promise<string> {
    const chatId = await deps.store.chatIdOf(commonConversationId);
    if (!chatId) {
        throw new Error(
            `lark outbound cannot resolve common_conversation_id=${commonConversationId}`,
        );
    }
    return chatId;
}

async function omIdOrThrow(
    deps: LarkDeliveryDeps,
    commonMessageId: string,
    kind = '',
): Promise<string> {
    const omId = await deps.store.omIdOf(commonMessageId);
    if (!omId) {
        throw new Error(`lark outbound cannot resolve ${kind}common_message_id=${commonMessageId}`);
    }
    return omId;
}

/**
 * 发出去，返回飞书给的新消息 id（**可能没有**，见 LarkSentMessage）。
 *
 * 三个分支：
 *
 *   part 0，被动   回复触发消息本身。**inThread 显式给 false** —— 普通聊天的回复
 *                  被挂进话题串，用户看到的是一个折叠起来的分支，等于没回
 *   part 0，主动   新发到会话（reply 的锚点根本不存在，见 resolveTarget）
 *   part > 0       新发到会话。续段不再挂在触发消息上，否则一次回答会在触发消息
 *                  底下摞出一串引用
 */
async function send(
    deps: LarkDeliveryDeps,
    response: LarkChatResponse,
    target: LarkTarget,
    partIndex: number,
    isProactive: boolean,
): Promise<string | undefined> {
    const post = await deps.render(response.content, {
        // 私聊不解析 @：里面没有第三个人，查一次群成员纯属白花一次查询。
        mentionChatId: response.is_p2p ? undefined : target.chatId,
        // 对象存储的永久句柄，原样往下递。签名只活 1.5 小时，所以现签在渲染那一步
        // 做（见 render.ts / pictures.ts）；老消息没有这个字段，就是不带图。
        pictureFileNames: response.picture_file_names,
    });

    if (partIndex === 0 && !isProactive) {
        return (await deps.api.replyPost(target.omId, post, false)).messageId;
    }
    return (await deps.api.sendPost(target.chatId, post)).messageId;
}

/**
 * 记下刚发出去的这条：公共层 assistant 行 ＋ 飞书映射，同一个事务。
 *
 * 返回落下去的 common_message_id —— 台账的 replies 指向它。
 */
async function record(
    deps: LarkDeliveryDeps,
    response: LarkChatResponse,
    target: LarkTarget,
    sentMessageId: string | undefined,
    botName: string,
    partIndex: number,
    isProactive: boolean,
): Promise<string> {
    // 主动发时把「这是她哪一次开口」记在行上。被动回复留空 —— 它不是任何一次
    // 主动开口的产物，而这一列的 NULL 正是"没有对应的一次开口"。
    const agentOutboundId = isProactive ? agentOutboundIdOf(response.message_id) : undefined;
    if (isProactive && !agentOutboundId) {
        // 静默留空会让那次开口在库里彻底失联，所以至少留一行能捞的记录。**不抛**：
        // 消息此刻已经发出去了。
        console.warn(
            `[lark-outbound] proactive message_id is not proactive:<uuid>, ` +
                `agent_outbound_id left null: ${response.message_id}`,
        );
    }

    // 飞书偶尔返回 code=0 却不带 message_id。落库的主键只能自己合成一个，而合成的
    // 东西必须能把**这一次发送**跟同一个会话里的下一次发送分开：
    //
    //   被动回复  锚点是触发消息的 om_id。它由信封里那个真实的公共层 message_id
    //             反查而来，一条触发消息对应一次回复，本来就唯一
    //   主动发    没有来源消息（target.omId 是空串），锚点是这次开口的 id。只用会话
    //             和段序的话，同一个会话里两次都没拿到 id 的主动发会算出同一个键，
    //             被下面那次反查复用成同一个 common_message_id、再被两条 or-ignore
    //             一起吃掉 —— 第二句话在公共层连行都没有，全程零报错
    //
    // 伪 id 形状不对时退回整串：记不下是哪次开口是一回事，让第二条消息在库里彻底
    // 消失是另一回事。om_id 列是 varchar(256)，锚点最长是 `proactive:<uuid>` 的 46 个
    // 字符，加上 `_part{段序}` 远够不着上限。
    const sendAnchor = isProactive ? agentOutboundId || response.message_id : target.omId;
    const omId = sentMessageId || `${sendAnchor}_part${partIndex}`;

    // 重投时复用已经铸过的 id，别再铸一个 —— 同一条飞书消息在公共层有两个身份，
    // 引用链会从中间断开。
    const existing = await deps.store.commonMessageIdOf(omId);
    const commonMessageId = existing ?? deps.newCommonId();

    // 主动发没有来源消息：message_id 是伪 id、root_id 也不是真实的公共层 id，
    // 两者都绝不能进公共层的引用链。root 留空时回落成自己（一条消息至少是自己
    // 这条话题的根）。
    const commonRootMessageId = isProactive
        ? response.root_id || undefined
        : response.root_id || response.message_id;
    const commonReplyMessageId = isProactive ? response.root_id || undefined : response.message_id;

    const eventTime = deps.now();
    const commonUserId = deps.botCommonUserId(botName);

    await deps.store.atomically(async (tables) => {
        await tables.insertCommonMessage({
            common_message_id: commonMessageId,
            channel: 'lark',
            common_conversation_id: response.chat_id,
            common_user_id: commonUserId,
            sender_display_name: deps.botDisplayName(botName),
            role: 'assistant',
            content: [{ kind: 'text', text: response.content }],
            content_text: response.content,
            common_root_message_id: commonRootMessageId ?? commonMessageId,
            common_reply_message_id: commonReplyMessageId,
            scope: response.is_p2p ? 'direct' : 'group',
            message_type: OUTBOUND_MESSAGE_TYPE,
            bot_name: botName,
            event_time: String(eventTime),
            response_id: response.session_id || undefined,
            agent_outbound_id: agentOutboundId,
        });

        await tables.insertLarkMessage({
            om_id: omId,
            common_message_id: commonMessageId,
            chat_id: target.chatId,
            message_type: OUTBOUND_MESSAGE_TYPE,
        });
    });

    return commonMessageId;
}

/**
 * 出错之后落一个 failed 终态。
 *
 * 这次写入自己再失败的话，吞不吞取决于 **spokeToLark** —— 本文件唯一一处"同一个
 * 动作两种失败处置"，所以分界线只在这里表达一次，别再散到各个分支上：
 *
 *   没调过飞书 API   往外抛。消息没发出去，重投安全；吞掉等于把一次数据库抖动
 *                    变成一条永远停在 pending 的台账
 *   调过（含调用抛错）吞掉。往外抛会让上游重投，真人收到第二条 —— 比台账停在
 *                    pending 严重得多
 */
async function settleFailed(
    deps: LarkDeliveryDeps,
    sessionId: string,
    spokeToLark: boolean,
): Promise<void> {
    if (!spokeToLark) {
        await deps.ledger.settle(sessionId, { status: 'failed' });
        return;
    }
    try {
        await deps.ledger.settle(sessionId, { status: 'failed' });
    } catch (error) {
        console.error(`[lark-outbound] failed to settle session=${sessionId}:`, error);
    }
}
