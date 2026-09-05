// chat-response-worker 的消息处理核心（平台无关、可注入依赖）。
//
// 把「消费一条 chat_response 消息」的全部业务逻辑从 worker 进程入口里抽出来，
// worker 入口（chat-response-worker.ts）只负责一次性的进程装配：起 logger、连
// DB / MQ、注册插件、起 metrics server，然后把真实依赖灌进 handleChatResponse。
// 逻辑搬到这里后，可以喂接近真实 MQ 的 payload 跑整条链做端到端测试，不必拉起
// 整个进程（chat-response-handler.proactive.test.ts）。

import { CommonAgentResponse } from '@inner/shared/entities';
import type { Repository } from 'typeorm';
import type { ConsumeMessage } from 'amqplib';
import dayjs from 'dayjs';

import { context } from '@middleware/context';
import { laneFromMessage } from '@inner/shared/mq-context';
import type { OutboundCapabilities } from '@inner/shared/channel';
import { dispatchChatResponseOutbound } from './chat-response-outbound';
import { resolveChatResponseOutboundRefs } from './chat-response-resolve';

// 出站走渠道能力端口：worker 只按 payload.channel 取插件，common id 反查、
// 平台富文本渲染、发送、outbound 映射落库都由当前 channel 的 capabilities 完成。

const SEND_DELAY_MS = 2500;

export interface ChatResponsePayload {
    // 出站分流的唯一依据，必填。生产者（agent-service 的
    // rabbitmq.channel_route_for_payload）同样 fail-closed，缺 channel 的 payload
    // 压根发不出来 —— 正常出站和 DLQ 重放都走这一条。
    channel: string;
    // 主动发（is_proactive）没有 agent_response 记录，session_id 为 null。
    session_id: string | null;
    message_id: string;
    chat_id: string;
    is_p2p: boolean;
    root_id?: string | null;
    user_id?: string;
    content: string;
    full_content?: string;
    /**
     * agent-service 带出来的图，值是对象存储的永久句柄（file_name）。
     *
     * **QQ 忽略它。** QQ 出站只发纯文本（CustomOutboundMessage 只有 text），本来就
     * 没有图片路径。声明在这里是因为线上真的有这个字段：不写下来的话，下一个人读
     * payload 会以为 agent-service 没发图，然后去上游找一个不存在的 bug。
     */
    picture_file_names?: string[];
    status: 'success' | 'failed';
    error?: string;
    // agent-service 仍在 body 里回填 lane（它自己按 body 字段做别的事），但
    // **判 lane 不看这里**：lane 只认 AMQP header，口径见
    // @inner/shared/mq-context 的 laneFromMessage（连同「为什么不回落 body」）。
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
    /**
     * 这条 channel 归不归本进程管。共库方案下 common_agent_response 没有 channel 列，
     * DB 层拦不住越界写入，隔离完全依赖「rk 分对了 + 消费侧不越界」。rk 配错是配置
     * 问题，这道校验让它立刻暴露而不是静默写脏另一个服务的台账。
     */
    ownsChannel: (channel: string) => boolean;
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
    const { repo, ownsChannel, getCapabilities, ack, nack, observeDuration, observeQueueDelay } =
        deps;

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

    // fail-closed 先于任何副作用：不属于自己的 channel 一行库都不查、一个插件都不取。
    // 拒绝 = nack(requeue=false)，prod 队列挂着 DLX，消息进 dead_letters 可查可重放；
    // requeue 只是把消息原样退回这条队列，下一轮还是本进程拿到，同一条消息在这里转圈。
    //
    // 缺 channel 跟「别人的 channel」同一个处置。这里曾经回落到飞书 —— 本服务已经没有
    // 飞书的出站能力，回落只会让消息被认领之后死在 getCapabilities 上，而 DLQ 里留下的
    // 是一条指向插件注册表的错误，看不出来源头是生产者少发了字段。
    const channel = payload.channel;
    if (typeof channel !== 'string' || channel.length === 0) {
        console.error(
            JSON.stringify({
                event: 'chat_response_channel_missing',
                session_id: payload.session_id ?? null,
                message_id: payload.message_id ?? null,
                consumer_tag: msg.fields?.consumerTag ?? null,
            }),
        );
        nack(msg, false);
        return;
    }
    if (!ownsChannel(channel)) {
        console.error(
            JSON.stringify({
                event: 'chat_response_foreign_channel',
                channel,
                session_id: payload.session_id ?? null,
                message_id: payload.message_id ?? null,
                consumer_tag: msg.fields?.consumerTag ?? null,
            }),
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
    const contextData = context.createContext(
        botName || undefined,
        undefined,
        laneFromMessage(msg),
    );

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
            // 插件做。sourceCommonMessageId 必须用 payload.message_id 这个【全局 id】，
            // 绝不是上面刚反查出来的 channelMessageId —— 插件拿它再去反查自己的私有
            // 映射（QQ 续段的回复锚点、出站幂等键），喂裸 id 进去必 miss，消息被静默
            // 吞掉。这条钉在 chat-response-worker.source-common-id.test.ts。
            // dispatch 据 part_index/proactive 选 reply(回复触发/root) 还是
            // sendText(新发)，返回新消息的渠道裸 id。
            //
            // 【已知残留 — MQ redeliver 不做发送级去重】
            // 主动发的 message_id（payload.message_id = 'proactive:<uuid5>'）在
            // agent-service 侧已是**整轮重投稳定**的派生键（life send_message 从本轮
            // act_id + 序号 uuid5 派生、life_wake 用 max_retries=1 关整轮重放 → 同一件
            // 主动发重投得同一段、(message_id, part_index) 稳定）。但本 worker 对 MQ
            // **redeliver** 不按这个稳定键做发送前去重：落库以平台返回的新消息 id 为主键
            //（recordOutboundMessage 的写入只挡同一个平台 id 的重复），发送本身没有「这个
            // proactive: 键我已发过吗」的前置查重。handleChatResponse 末尾**无条件 ack**
            //（连出站失败也 ack），所以唯一的 redeliver 窗口是 worker 在发送之后、ack
            // 之前**崩溃**——重启重投会再发一次、真人收到两条。
            // 这是 chat_response 链路**系统级**的 at-least-once 属性，**真人回复路径同样
            // 存在**。这里不修：要修需引入发送级幂等（从 proactive uuid5 派生确定性
            // common_message_id + 发送前存在性查重 + 强制该 id 落库），跨服务、动共享写
            // 路径、有 cutover 风险，留作后续。
            const tSend0 = Date.now();
            const sentRef = await dispatchChatResponseOutbound(capabilities, {
                content,
                channelMessageId,
                channelConversationId,
                channelRootMessageId,
                sourceCommonMessageId: message_id,
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
