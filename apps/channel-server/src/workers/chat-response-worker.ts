/**
 * Chat Response Worker — 独立进程
 *
 * 消费 RabbitMQ chat_response queue，
 * 按 part_index 直接发送 post 消息到飞书，
 * 每条消息发送后立即存 common_message/lark_message 并追加 common_agent_response.replies，
 * is_last 时更新 response_text 和状态为 completed。
 */

import { LoggerFactory } from '@inner/shared';

LoggerFactory.createLogger({
    enableFileLogging: true,
    logDir: process.env.LOG_DIR || '/var/log/channel-server',
    logFileName: 'chat-response-worker.log',
    enableConsoleOverride: true,
});

import { createServer } from 'http';
import AppDataSource from 'ormconfig';
import { CommonAgentResponse } from '@entities/common-agent-response';
import {
    rabbitmqClient,
    CHAT_RESPONSE,
    getLane,
    laneQueue,
} from '@integrations/rabbitmq';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { larkCredentials } from '@core/services/bot/lark-credentials';
import { initializeLarkClients } from '@integrations/lark-client';
import { context } from '@middleware/context';
import { reverseResolveOutbound } from '@plugins/lark/outbound-reverse-resolve';
import { storeLarkOutboundMessage } from '@plugins/lark/common-projector';
import { getChannelRegistry } from '@core/registry/channel-registry';
import '@plugins/index';
import { imageRegistryLookupId } from './image-registry-key';
import { dispatchChatResponseOutbound } from './chat-response-outbound';
import dayjs from 'dayjs';
import { Histogram, Registry, collectDefaultMetrics } from 'prom-client';
import type { ConsumeMessage } from 'amqplib';

// 出站走渠道能力端口（B3）：飞书富文本/图片渲染 + send/reply 收进 plugins/lark
// 的 OutboundCapabilities，worker 不再 import 任何飞书 SDK。import '@plugins/index'
// 触发飞书插件自注册（进 ChannelRegistry），下方 getChannelRegistry().get('lark')
// 取其 capabilities。当前出站链路只服务飞书；T6 接其他 channel 时按 payload.channel
// 取对应插件。
const OUTBOUND_CHANNEL = 'lark';

// Metrics (chat-response-worker is a standalone process, needs its own registry)
const metricsRegistry = new Registry();
collectDefaultMetrics({ register: metricsRegistry });

const chatResponseDuration = new Histogram({
    name: 'chat_response_duration_seconds',
    help: 'Duration of each chat-response-worker stage',
    labelNames: ['stage'] as const,  // db_query, resolve, lark_send, db_write, total
    buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
    registers: [metricsRegistry],
});

const chatResponseQueueDelay = new Histogram({
    name: 'chat_response_queue_delay_seconds',
    help: 'Time spent waiting in MQ queue (chat_response)',
    buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
    registers: [metricsRegistry],
});

const SEND_DELAY_MS = 2500;

interface ChatResponsePayload {
    session_id: string;
    message_id: string;
    chat_id: string;
    is_p2p: boolean;
    root_id?: string;
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
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function handleChatResponse(msg: ConsumeMessage): Promise<void> {
    const tStart = Date.now();
    let payload: ChatResponsePayload;
    try {
        payload = JSON.parse(msg.content.toString());
    } catch (e) {
        console.error('[ChatResponseWorker] Malformed message, sending to DLQ:', msg.content.toString().slice(0, 200));
        rabbitmqClient.nack(msg, false);
        return;
    }

    const publishedAt = (payload as any).published_at as number | undefined;
    const queueDelayMs = publishedAt ? tStart - publishedAt : -1;
    if (queueDelayMs > 0) {
        chatResponseQueueDelay.observe(queueDelayMs / 1000);
    }

    const {
        session_id,
        message_id,
        chat_id,
        is_p2p,
        root_id,
        user_id,
        content,
        full_content,
        status,
        error,
        part_index = 0,
        is_last = false,
        is_proactive = false,
    } = payload;

    console.info(
        `[ChatResponseWorker] Processing: session_id=${session_id}, status=${status}, part=${part_index}, is_last=${is_last}, queue_delay=${queueDelayMs}ms`,
    );

    const repo = AppDataSource.getRepository(CommonAgentResponse);

    // 查询 agent_response 获取 bot_name
    const tDbQuery0 = Date.now();
    const agentResponse = await repo.findOneBy({ session_id });
    const dbQueryMs = Date.now() - tDbQuery0;
    chatResponseDuration.labels({ stage: 'db_query' }).observe(dbQueryMs / 1000);

    // payload.bot_name 由 agent-service 按 persona_id 反查，优先使用
    const botName = payload.bot_name || agentResponse?.bot_name;
    if (!botName) {
        console.error(`[ChatResponseWorker] No bot_name found: session_id=${session_id}, is_proactive=${is_proactive}`);
        rabbitmqClient.ack(msg);
        return;
    }

    // 设置 bot context — ack 统一在 context.run 之后，callback 内部禁止 ack/nack
    const contextData = context.createContext(botName || undefined, undefined, payload.lane);

    await context.run(contextData, async () => {
        if (status === 'failed') {
            console.error(
                `[ChatResponseWorker] Agent failed: session_id=${session_id}, error=${error}`,
            );
            await repo.update({ session_id }, { status: 'failed' });
            return;
        }

        if (!content) {
            console.warn(`[ChatResponseWorker] Empty content: session_id=${session_id}, part=${part_index}`);
            if (is_last && agentResponse) {
                await repo.update({ session_id }, { status: 'completed' });
            }
            return;
        }

        try {
            // ---- 出站反查（common_*_id → 飞书裸 id）----
            // ChatTrigger/ChatResponseSegment 入站走过 lark common projector，
            // message_id / chat_id / root_id 是 common_*_id。这里由 lark 插件
            // 读取 lark_* 映射反查回飞书裸 id，构造能力端口要的渠道内 ref。反查不到
            // 明确抛错（落入下方 catch），绝不静默把回复发到错地方。
            const rr = await reverseResolveOutbound({
                commonMessageId: message_id,
                commonConversationId: chat_id,
                commonRootMessageId: root_id || undefined,
            });
            const larkMessageId = rr.channelMessageId;
            const larkChatId = rr.channelChatId;
            const larkRootId = rr.channelRootId;

            // part > 0 续段：发送前节流（与现状一致，worker 侧出站节奏，非渲染）。
            if (part_index > 0) {
                await sleep(SEND_DELAY_MS);
            }

            // ---- 出站走渠道能力端口（B3）----
            // content 是 AI 原始 markdown（平台无关）；飞书富文本渲染
            // （@N.png 上传飞书、@用户名 mention、markdown→PostContent、send/reply）
            // 由 lark 插件的 OutboundCapabilities 内部做，worker 不碰飞书结构/SDK。
            // RenderContext：
            //   imageRegistryId 必须用【全局 message_id】（agent-service 注册图片用的
            //     同一个 key，见 image-registry-key.ts）——绝不能用反查后的飞书裸 om_*，
            //     那个键从没写过、会 miss → 图片被静默吞掉。故它走渲染上下文、不混进渠道 ref。
            //   larkChatId 群 mention 解析所需的飞书裸 chatId；
            //   resolveMentions 群聊解析 @用户名、私聊跳过（与现状 is_p2p 跳过一致，
            //     由 dispatch 据 isP2p 决定）。
            // dispatch 据 part_index/proactive 选 reply(回复触发/root) 还是 sendText(新发)，
            // 返回新消息的渠道裸 id。
            const tSend0 = Date.now();
            const capabilities = getChannelRegistry().get(OUTBOUND_CHANNEL).capabilities;
            const sentRef = await dispatchChatResponseOutbound(capabilities, {
                content,
                larkMessageId,
                larkChatId,
                larkRootId,
                imageRegistryId: imageRegistryLookupId(payload),
                isP2p: is_p2p,
                partIndex: part_index,
                isProactive: is_proactive,
            });
            const sendMs = Date.now() - tSend0;
            chatResponseDuration.labels({ stage: 'lark_send' }).observe(sendMs / 1000);

            const aiMessageId = sentRef.channelId || undefined;
            const effectiveLarkMessageId = aiMessageId || `${larkMessageId}_part${part_index}`;
            const botConfig = multiBotManager.getBotConfig(botName);
            const senderDisplayName =
                botConfig?.channel === OUTBOUND_CHANNEL
                    ? (multiBotManager.getDisplayNameByAppId(
                          larkCredentials(botConfig).app_id,
                      ) ?? undefined)
                    : undefined;

            // 每条消息发完后立即存 common_message + lark_message
            const tDbWrite0 = Date.now();
            const now = dayjs().valueOf();
            const commonAssistantMessageId = await storeLarkOutboundMessage({
                omId: effectiveLarkMessageId,
                chatId: larkChatId,
                commonConversationId: chat_id,
                commonRootMessageId: is_proactive ? root_id : (root_id || message_id),
                commonReplyMessageId: is_proactive ? root_id : message_id,
                contentText: content,
                botName,
                senderDisplayName,
                scope: is_p2p ? 'direct' : 'group',
                eventTime: now,
                messageType: 'post',
                responseId: session_id,
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
                        { session_id },
                        {
                            response_text: full_content || content,
                            status: 'completed',
                        },
                    );
                }
            }
            const dbWriteMs = Date.now() - tDbWrite0;
            chatResponseDuration.labels({ stage: 'db_write' }).observe(dbWriteMs / 1000);

            console.info(`[ChatResponseWorker] Reply sent: session_id=${session_id}, part=${part_index}, ai_msg_id=${effectiveLarkMessageId}`);

            const totalMs = Date.now() - tStart;
            chatResponseDuration.labels({ stage: 'total' }).observe(totalMs / 1000);
            console.info(JSON.stringify({
                event: 'chat_response_done',
                session_id,
                part_index,
                queue_ms: queueDelayMs,
                db_query_ms: dbQueryMs,
                send_ms: sendMs,
                db_write_ms: dbWriteMs,
                total_ms: totalMs,
            }));
        } catch (e) {
            console.error(`[ChatResponseWorker] Failed to send reply: session_id=${session_id}, part=${part_index}`, e);
            try {
                await repo.update({ session_id }, { status: 'failed' });
            } catch (dbErr) {
                console.error(`[ChatResponseWorker] DB update also failed: session_id=${session_id}`, dbErr);
            }
        }
    });

    rabbitmqClient.ack(msg);
}

async function main(): Promise<void> {
    console.info('[ChatResponseWorker] Starting...');

    // 1. 初始化数据库
    await AppDataSource.initialize();
    console.info('[ChatResponseWorker] Database connected');

    // 2. 初始化 Lark 客户端
    await multiBotManager.initialize();
    await initializeLarkClients();
    console.info('[ChatResponseWorker] Lark clients initialized');

    // 3. 连接 RabbitMQ 并声明拓扑
    await rabbitmqClient.connect();
    await rabbitmqClient.declareTopology();
    console.info('[ChatResponseWorker] RabbitMQ connected');

    // 4. 开始消费
    const lane = getLane();
    const queue = laneQueue(CHAT_RESPONSE.queue, lane);
    await rabbitmqClient.consume(queue, handleChatResponse);
    console.info(
        `[ChatResponseWorker] Consuming queue: ${queue}, waiting for messages...`,
    );

    // 5. 暴露 Prometheus metrics
    const metricsPort = parseInt(process.env.METRICS_PORT || '9091', 10);
    createServer(async (_req, res) => {
        res.setHeader('Content-Type', metricsRegistry.contentType);
        res.end(await metricsRegistry.metrics());
    }).listen(metricsPort, () => {
        console.info(`[ChatResponseWorker] Metrics server on :${metricsPort}`);
    });
}

main().catch((err) => {
    console.error('[ChatResponseWorker] Fatal error:', err);
    process.exit(1);
});

// 优雅关闭
process.on('SIGINT', async () => {
    await rabbitmqClient.close();
    await AppDataSource.destroy();
    process.exit(0);
});

process.on('SIGTERM', async () => {
    await rabbitmqClient.close();
    await AppDataSource.destroy();
    process.exit(0);
});
