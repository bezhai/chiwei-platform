/**
 * Chat Response Worker — 独立进程
 *
 * 消费 RabbitMQ chat_response queue，
 * 按 part_index 直接发送 post 消息到飞书，
 * 每条消息发送后立即存 conversation_messages 并追加 agent_responses.replies，
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
import { AgentResponse } from '@entities/agent-response';
import {
    rabbitmqClient,
    CHAT_RESPONSE,
    getLane,
    laneQueue,
} from '@integrations/rabbitmq';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { initializeLarkClients, uploadImage } from '@integrations/lark-client';
import { context } from '@middleware/context';
import { storeMessage } from '@integrations/memory';
import { replyPost, sendPost } from '@lark/basic/message';
import { markdownToPostContent } from 'core/services/message/post-content-processor';
import { resolveMentionsForGroup } from 'core/services/message/resolve-mentions';
import { MessageContentUtils } from 'core/models/message-content';
import { reverseResolveForLark } from '@core/channels/outbound-pipeline';
import { getIdentityResolver } from '@integrations/identity-resolver-runtime';
import { hgetall } from '@cache/redis-client';
import dayjs from 'dayjs';
import { Readable } from 'stream';
import { Counter, Histogram, Registry, collectDefaultMetrics } from 'prom-client';
import type { ConsumeMessage } from 'amqplib';

// Metrics (chat-response-worker is a standalone process, needs its own registry)
const metricsRegistry = new Registry();
collectDefaultMetrics({ register: metricsRegistry });

const imageResolveDuration = new Histogram({
    name: 'image_resolve_step_duration_seconds',
    help: 'Duration of each image resolve step',
    labelNames: ['step'] as const,  // redis, download_tos, upload_lark
    registers: [metricsRegistry],
});

const imageResolveTotal = new Counter({
    name: 'image_resolve_requests_total',
    help: 'Total image resolve outcomes',
    labelNames: ['status'] as const,  // success, not_found, download_failed, upload_failed
    registers: [metricsRegistry],
});

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
const IMAGE_REF_PATTERN = /!\[([^\]]*)\]\(@?(\d+\.png)\)/g;

/**
 * Resolve @N.png references in markdown image syntax.
 * Downloads TOS URL → uploads to Lark → replaces with image_key.
 */
async function resolveImageReferences(content: string, messageId: string): Promise<string> {
    const matches = [...content.matchAll(IMAGE_REF_PATTERN)];
    if (matches.length === 0) return content;

    const tStart = Date.now();

    // Load registry from Redis
    const registry = await hgetall(`image_registry:${messageId}`);
    const tRedis = (Date.now() - tStart) / 1000;
    imageResolveDuration.labels({ step: 'redis' }).observe(tRedis);

    if (!registry || Object.keys(registry).length === 0) {
        console.warn(`[ChatResponseWorker] No image registry found for message_id=${messageId}`);
        return content;
    }

    let result = content;

    // Resolve single image: download TOS → upload Lark → return replacement
    async function resolveSingle(match: RegExpExecArray): Promise<{ fullMatch: string; replacement: string }> {
        const fullMatch = match[0];
        const alt = match[1];
        const filename = match[2];

        const tosUrl = registry[filename];
        if (!tosUrl) {
            console.warn(`[ChatResponseWorker] Image ${filename} not found in registry`);
            imageResolveTotal.labels({ status: 'not_found' }).inc();
            return { fullMatch, replacement: `(图片 ${filename} 不可用)` };
        }

        try {
            // Download from TOS
            const tDl0 = Date.now();
            const response = await fetch(tosUrl);
            if (!response.ok) {
                console.error(`[ChatResponseWorker] Failed to download ${filename}: ${response.status}`);
                imageResolveTotal.labels({ status: 'download_failed' }).inc();
                return { fullMatch, replacement: `(图片 ${filename} 下载失败)` };
            }

            const buffer = Buffer.from(await response.arrayBuffer());
            const tDownload = (Date.now() - tDl0) / 1000;
            imageResolveDuration.labels({ step: 'download_tos' }).observe(tDownload);

            // Upload to Lark
            const tUp0 = Date.now();
            const stream = Readable.from(buffer);
            const uploadResult = await uploadImage(stream);
            const imageKey = uploadResult?.image_key || uploadResult?.data?.image_key;
            const tUpload = (Date.now() - tUp0) / 1000;
            imageResolveDuration.labels({ step: 'upload_lark' }).observe(tUpload);

            if (!imageKey) {
                console.error(`[ChatResponseWorker] Failed to upload ${filename} to Lark, response:`, JSON.stringify(uploadResult));
                imageResolveTotal.labels({ status: 'upload_failed' }).inc();
                return { fullMatch, replacement: `(图片 ${filename} 上传失败)` };
            }

            imageResolveTotal.labels({ status: 'success' }).inc();
            console.info(
                `[ChatResponseWorker] Resolved ${filename} -> ${imageKey} ` +
                `(size=${Math.round(buffer.length / 1024)}KB download=${Math.round(tDownload * 1000)}ms upload=${Math.round(tUpload * 1000)}ms)`,
            );
            return { fullMatch, replacement: `![${alt}](${imageKey})` };
        } catch (e) {
            console.error(`[ChatResponseWorker] Error resolving ${filename}:`, e);
            return { fullMatch, replacement: `(图片 ${filename} 处理失败)` };
        }
    }

    // Process in batches of 5 concurrently
    const CONCURRENCY = 5;
    for (let i = 0; i < matches.length; i += CONCURRENCY) {
        const batch = matches.slice(i, i + CONCURRENCY);
        const results = await Promise.all(batch.map(m => resolveSingle(m)));
        for (const { fullMatch, replacement } of results) {
            result = result.replace(fullMatch, replacement);
        }
    }

    const tTotal = Date.now() - tStart;
    console.info(
        `[ChatResponseWorker] resolveImageReferences done: ${matches.length} refs, ` +
        `redis=${Math.round(tRedis * 1000)}ms total=${tTotal}ms`,
    );

    return result;
}

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

    const repo = AppDataSource.getRepository(AgentResponse);

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
            // ---- 出站反查（5b 边界点 6）----
            // ChatTrigger/ChatResponseSegment 入站若走过渠道契约链，message_id
            // /chat_id/root_id 是全局 internal_*_id。这里用 IdentityResolver
            // .toChannel 反查回飞书裸 ID，再喂给现状飞书富文本出站路径
            // （sendPost/replyPost + 识图/markdown→post 全部保留不动——飞书
            // native 出站交互，不塞进纯文本 OutboundAdapter，否则丢图片/格式，
            // 是「飞书非文字/看图聊天零截断」硬约束不允许的回归）。反查不到
            // 明确抛错（落入下方 catch），绝不静默把回复发到错地方。
            const reverseResolver = getIdentityResolver();
            // channel === 'lark' 边界断言在 reverseResolveForLark 内做：
            // T6 接 QQ 后，误把非飞书全局 ID 喂飞书富文本发送器会在此炸出来，
            // 绝不静默发到错地方（fail-loud 出站对偶）。
            const rr = await reverseResolveForLark({
                resolver: reverseResolver,
                messageGlobalId: message_id,
                chatGlobalId: chat_id,
                rootGlobalId: root_id || undefined,
            });
            const larkMessageId = rr.channelMessageId;
            const larkChatId = rr.channelChatId;
            const larkRootId = rr.channelRootId;

            // 群聊中将 @用户名 替换为 <at union_id="xxx">用户名</at>
            // 解析 @N.png 引用 → 下载 TOS → 上传飞书 → 替换为 image_key
            // 用反查回的飞书裸 ID（resolveMentionsForGroup 走飞书群成员、
            // resolveImageReferences 走 redis image_registry，均按飞书裸键）。
            const tResolve0 = Date.now();
            let resolvedContent = is_p2p
                ? content
                : await resolveMentionsForGroup(content, larkChatId);
            resolvedContent = await resolveImageReferences(resolvedContent, larkMessageId);
            const resolveMs = Date.now() - tResolve0;
            chatResponseDuration.labels({ stage: 'resolve' }).observe(resolveMs / 1000);

            const postContent = markdownToPostContent(resolvedContent);

            // 发送消息并捕获 AI 消息 ID（飞书裸 ID 寻址，行为与现状一致）
            const tSend0 = Date.now();
            let aiMessageId: string | undefined;
            if (part_index === 0) {
                if (is_proactive) {
                    // proactive: reply to real message if available, else send new
                    if (larkRootId) {
                        aiMessageId = await replyPost(larkRootId, postContent);
                    } else {
                        aiMessageId = await sendPost(larkChatId, postContent);
                    }
                } else {
                    aiMessageId = await replyPost(larkMessageId, postContent);
                }
            } else {
                await sleep(SEND_DELAY_MS);
                aiMessageId = await sendPost(larkChatId, postContent);
            }
            const sendMs = Date.now() - tSend0;
            chatResponseDuration.labels({ stage: 'lark_send' }).observe(sendMs / 1000);

            const effectiveLarkMessageId = aiMessageId || `${larkMessageId}_part${part_index}`;
            // bot 自己的回复消息也全局化（与入站写入点全局化一致）：把这条新
            // 飞书消息 id 正查分配/取全局 internal_message_id 后再落库，保证
            // conversation_messages 的 assistant 行身份列同样是全局 ID。
            // reverseResolveForLark 已断言 channel==='lark'，这里出站发的是
            // 飞书 native 富文本，新消息 id 是飞书裸 id，按 (lark, 裸id) 正查
            // 分配全局 internal_message_id（与入站写入点全局化一致）。
            const globalAssistantMessageId = await reverseResolver.resolve(
                'message',
                'lark',
                effectiveLarkMessageId,
            );

            // 每条消息发完后立即存 conversation_messages（身份列写全局 ID）
            const tDbWrite0 = Date.now();
            const now = dayjs().valueOf();
            await storeMessage({
                user_id: botName || context.getBotName() || '',
                content: MessageContentUtils.wrapMarkdownAsV2(content),
                role: 'assistant',
                message_id: globalAssistantMessageId,
                message_type: 'post',
                chat_id: chat_id,
                chat_type: is_p2p ? 'p2p' : 'group',
                create_time: String(now),
                root_message_id: is_proactive ? (root_id || globalAssistantMessageId) : (root_id || message_id),
                reply_message_id: is_proactive ? (root_id || undefined) : message_id,
                response_id: session_id,
            });

            // proactive 没有 agent_response 记录，跳过 replies 追加和状态更新
            if (agentResponse) {
                // agent_responses.replies[].message_id 由 5c-scope 读取方
                // （recall / delete-message）按飞书裸 message id 消费。本步
                // 不动这些读取方，故此处保持飞书裸 id（effectiveLarkMessageId），
                // 全局化留给 5c，不在本步擅自切。
                const replyEntry = [
                    {
                        message_id: effectiveLarkMessageId,
                        content_type: 'post',
                        sent_at: new Date().toISOString(),
                    },
                ];
                await repo
                    .createQueryBuilder()
                    .update(AgentResponse)
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
                resolve_ms: resolveMs,
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
