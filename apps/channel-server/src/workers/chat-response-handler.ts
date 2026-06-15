// chat-response-worker 的消息处理核心（平台无关、可注入依赖）。
//
// 把「消费一条 chat_response 消息」的全部业务逻辑从 worker 进程入口里抽出来，
// worker 入口（chat-response-worker.ts）只负责一次性的进程装配：起 logger、连
// DB / MQ、注册插件、起 metrics server，然后把真实依赖灌进 handleChatResponse。
// 逻辑搬到这里后，可以喂接近真实 MQ 的 payload 跑整条链做端到端测试，不必拉起
// 整个进程（chat-response-handler.proactive.test.ts）。

import { CommonAgentResponse } from '@entities/common-agent-response';
import type { Repository } from 'typeorm';
import type { ConsumeMessage } from 'amqplib';
import dayjs from 'dayjs';

import { context } from '@middleware/context';
import type { OutboundCapabilities } from '@core/ports/channel-plugin';
import { imageRegistryLookupId } from './image-registry-key';
import { dispatchChatResponseOutbound } from './chat-response-outbound';
import { resolveChatResponseOutboundRefs } from './chat-response-resolve';

// 出站走渠道能力端口：worker 只按 payload.channel 取插件，common id 反查、
// 平台富文本渲染、发送、outbound 映射落库都由当前 channel 的 capabilities 完成。
// 旧 MQ/outbox 残留不带 channel 的 payload 仍按 lark 处理。
const DEFAULT_CHANNEL = 'lark';

const SEND_DELAY_MS = 2500;

export interface ChatResponsePayload {
    channel?: string;
    // 主动发（is_proactive）没有 agent_response 记录，session_id 为 null。
    session_id: string | null;
    message_id: string;
    chat_id: string;
    is_p2p: boolean;
    root_id?: string | null;
    user_id?: string;
    content: string;
    full_content?: string;
    status: 'success' | 'failed';
    error?: string;
    lane?: string;
    part_index?: number;
    is_last?: boolean;
    is_proactive?: boolean;
    bot_name?: string;
    // 主动发由 agent-service 按 persona 触发，persona_id 用于出站失败时排查定位。
    persona_id?: string;
    published_at?: number;
}

// metrics 阶段标签（与 chat-response-worker 进程级 Histogram 的 labelNames 对齐）。
export type ChatResponseStage =
    | 'db_query'
    | 'resolve'
    | 'channel_send'
    | 'db_write'
    | 'total';

// handler 的可注入依赖。worker 入口灌真实实现，测试灌 spy。
export interface ChatResponseHandlerDeps {
    repo: Repository<CommonAgentResponse>;
    getCapabilities: (channel: string) => OutboundCapabilities;
    ack: (msg: ConsumeMessage) => void;
    nack: (msg: ConsumeMessage, requeue?: boolean) => void;
    observeDuration: (stage: ChatResponseStage, seconds: number) => void;
    observeQueueDelay: (seconds: number) => void;
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function handleChatResponse(
    deps: ChatResponseHandlerDeps,
    msg: ConsumeMessage,
): Promise<void> {
    const { repo, getCapabilities, ack, nack, observeDuration, observeQueueDelay } = deps;

    const tStart = Date.now();
    let payload: ChatResponsePayload;
    try {
        payload = JSON.parse(msg.content.toString());
    } catch (e) {
        console.error(
            '[ChatResponseWorker] Malformed message, sending to DLQ:',
            msg.content.toString().slice(0, 200),
        );
        nack(msg, false);
        return;
    }

    const publishedAt = payload.published_at;
    const queueDelayMs = publishedAt ? tStart - publishedAt : -1;
    if (queueDelayMs > 0) {
        observeQueueDelay(queueDelayMs / 1000);
    }

    const {
        session_id,
        message_id,
        chat_id,
        is_p2p,
        root_id,
        content,
        full_content,
        status,
        error,
        part_index = 0,
        is_last = false,
        is_proactive = false,
        channel = DEFAULT_CHANNEL,
        persona_id,
    } = payload;

    console.info(
        `[ChatResponseWorker] Processing: session_id=${session_id}, channel=${channel}, status=${status}, part=${part_index}, is_last=${is_last}, is_proactive=${is_proactive}, queue_delay=${queueDelayMs}ms`,
    );

    // 查询 agent_response 获取 bot_name。主动发（is_proactive）没有 agent_response
    // 记录、session_id 为空，findOneBy({session_id: null/undefined}) 是非法/会误匹配，
    // 直接跳过查询：bot_name 由 payload.bot_name 给（agent-service 按 persona_id 反查）。
    const tDbQuery0 = Date.now();
    const agentResponse = session_id ? await repo.findOneBy({ session_id }) : null;
    const dbQueryMs = Date.now() - tDbQuery0;
    observeDuration('db_query', dbQueryMs / 1000);

    // payload.bot_name 由 agent-service 按 persona_id 反查，优先使用
    const botName = payload.bot_name || agentResponse?.bot_name;
    if (!botName) {
        console.error(
            `[ChatResponseWorker] No bot_name found: session_id=${session_id}, is_proactive=${is_proactive}`,
        );
        ack(msg);
        return;
    }

    // 设置 bot context — ack 统一在 context.run 之后，callback 内部禁止 ack/nack
    const contextData = context.createContext(botName || undefined, undefined, payload.lane);

    await context.run(contextData, async () => {
        if (status === 'failed') {
            console.error(
                `[ChatResponseWorker] Agent failed: session_id=${session_id}, error=${error}`,
            );
            // proactive 无 agent_response 记录、session_id 空，无可更新的状态行。
            if (agentResponse) {
                await repo.update({ session_id: session_id! }, { status: 'failed' });
            }
            return;
        }

        if (!content) {
            console.warn(
                `[ChatResponseWorker] Empty content: session_id=${session_id}, part=${part_index}`,
            );
            if (is_last && agentResponse) {
                await repo.update({ session_id: session_id! }, { status: 'completed' });
            }
            return;
        }

        try {
            const capabilities = getCapabilities(channel);

            // ---- 出站反查（common_*_id → 当前 channel 裸 id）----
            // ChatTrigger/ChatResponseSegment 只携带 common_*_id。这里经当前 channel
            // 插件读取自己的私有映射，构造能力端口要的渠道内 ref。反查不到明确
            // 抛错（落入下方 catch），绝不静默把回复发到错地方。
            //
            // 被动回复走完整反查（source message + conversation + root）。主动发
            // （is_proactive）没有来源消息、message_id 是伪 proactive: id，跳过来源
            // 消息反查，只解析会话、往这个真实 p2p 会话新发一条（见 chat-response-resolve.ts）。
            const refs = await resolveChatResponseOutboundRefs(capabilities, {
                isProactive: is_proactive,
                messageId: message_id,
                chatId: chat_id,
                rootId: root_id || undefined,
            });
            const channelConversationId = refs.channelConversationId;
            const channelMessageId = refs.channelMessageId;
            const channelRootMessageId = refs.channelRootMessageId;

            // part > 0 续段：发送前节流（与现状一致，worker 侧出站节奏，非渲染）。
            if (part_index > 0) {
                await sleep(SEND_DELAY_MS);
            }

            // ---- 出站走渠道能力端口 ----
            // content 是 AI 原始 markdown（平台无关）；平台富文本渲染由当前 channel
            // 插件做。imageRegistryId 必须用【全局 message_id】（见 image-registry-key.ts）。
            // dispatch 据 part_index/proactive 选 reply(回复触发/root) 还是
            // sendText(新发)，返回新消息的渠道裸 id。
            const tSend0 = Date.now();
            const sentRef = await dispatchChatResponseOutbound(capabilities, {
                content,
                channelMessageId,
                channelConversationId,
                channelRootMessageId,
                imageRegistryId: imageRegistryLookupId(payload),
                isP2p: is_p2p,
                partIndex: part_index,
                isProactive: is_proactive,
            });
            const sendMs = Date.now() - tSend0;
            observeDuration('channel_send', sendMs / 1000);

            const aiMessageId = sentRef.channelId || undefined;
            const effectiveChannelMessageId =
                aiMessageId || `${channelMessageId}_part${part_index}`;

            // 每条消息发完后立即存 common_message + channel 私有映射。
            // 主动发没有来源消息：root_id / message_id 都不是真实 common id（message_id
            // 是 proactive: 伪 id），绝不能写进 common root/reply 映射；留空即可。
            const tDbWrite0 = Date.now();
            const now = dayjs().valueOf();
            const commonAssistantMessageId = await capabilities.recordOutboundMessage({
                channelMessageId: effectiveChannelMessageId,
                channelConversationId,
                commonConversationId: chat_id,
                commonRootMessageId: is_proactive
                    ? root_id || undefined
                    : root_id || message_id,
                commonReplyMessageId: is_proactive ? root_id || undefined : message_id,
                contentText: content,
                botName,
                scope: is_p2p ? 'direct' : 'group',
                eventTime: now,
                messageType: 'post',
                // 主动发 session_id 为 null：不挂 responseId（没有对应 agent_response 行）。
                responseId: session_id || undefined,
            });

            // proactive 没有 agent_response 记录，跳过 replies 追加和状态更新
            if (agentResponse) {
                const replyEntry = [
                    {
                        common_message_id: commonAssistantMessageId,
                        content_type: 'post',
                        sent_at: new Date().toISOString(),
                    },
                ];
                await repo
                    .createQueryBuilder()
                    .update(CommonAgentResponse)
                    .set({
                        replies: () =>
                            `COALESCE(replies, '[]'::jsonb) || :replyEntry::jsonb`,
                    })
                    .setParameter('replyEntry', JSON.stringify(replyEntry))
                    .where('session_id = :sid', { sid: session_id })
                    .execute();

                if (is_last) {
                    await repo.update(
                        { session_id: session_id! },
                        {
                            response_text: full_content || content,
                            status: 'completed',
                        },
                    );
                }
            }
            const dbWriteMs = Date.now() - tDbWrite0;
            observeDuration('db_write', dbWriteMs / 1000);

            console.info(
                `[ChatResponseWorker] Reply sent: session_id=${session_id}, channel=${channel}, part=${part_index}, ai_msg_id=${effectiveChannelMessageId}`,
            );

            const totalMs = Date.now() - tStart;
            observeDuration('total', totalMs / 1000);
            console.info(
                JSON.stringify({
                    event: 'chat_response_done',
                    session_id,
                    part_index,
                    queue_ms: queueDelayMs,
                    db_query_ms: dbQueryMs,
                    send_ms: sendMs,
                    db_write_ms: dbWriteMs,
                    total_ms: totalMs,
                }),
            );
        } catch (e) {
            // 出站失败：记 error 级显眼日志，带够排查的字段（chat_id / bot_name /
            // persona_id / channel / part / is_proactive），别静默吞。异步失败回流
            // （把发不出去的消息重投 / 告警）是下一刀的事；这一刀只保证失败可见、
            // 能在日志里直接定位是哪条主动发 / 哪个会话发不出去。
            console.error(
                JSON.stringify({
                    event: 'chat_response_outbound_failed',
                    session_id,
                    channel,
                    chat_id,
                    bot_name: botName,
                    persona_id: persona_id ?? null,
                    part_index,
                    is_proactive,
                    error: e instanceof Error ? e.message : String(e),
                }),
                e,
            );
            // proactive 无 agent_response 记录、session_id 空，无可更新的状态行。
            if (agentResponse) {
                try {
                    await repo.update({ session_id: session_id! }, { status: 'failed' });
                } catch (dbErr) {
                    console.error(
                        `[ChatResponseWorker] DB update also failed: session_id=${session_id}`,
                        dbErr,
                    );
                }
            }
        }
    });

    ack(msg);
}
