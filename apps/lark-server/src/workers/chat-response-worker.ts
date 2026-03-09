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
    logDir: process.env.LOG_DIR || '/var/log/lark-server',
    logFileName: 'chat-response-worker.log',
    enableConsoleOverride: true,
});

import AppDataSource from 'ormconfig';
import { AgentResponse } from '@entities/agent-response';
import {
    rabbitmqClient,
    QUEUE_CHAT_RESPONSE,
    getLane,
    laneQueue,
} from '@integrations/rabbitmq';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { initializeLarkClients } from '@integrations/lark-client';
import { context } from '@middleware/context';
import { storeMessage } from '@integrations/memory';
import { replyPost, sendPost } from '@lark/basic/message';
import { markdownToPostContent } from 'core/services/message/post-content-processor';
import { resolveMentionsForGroup } from 'core/services/message/resolve-mentions';
import { getBotUnionId } from '@core/services/bot/bot-var';
import { MessageContentUtils } from 'core/models/message-content';
import dayjs from 'dayjs';
import type { ConsumeMessage } from 'amqplib';

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
}

function sleep(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
}

async function handleChatResponse(msg: ConsumeMessage): Promise<void> {
    const payload: ChatResponsePayload = JSON.parse(msg.content.toString());
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
        is_last = true,
    } = payload;

    console.info(
        `[ChatResponseWorker] Processing: session_id=${session_id}, status=${status}, part=${part_index}, is_last=${is_last}`,
    );

    const repo = AppDataSource.getRepository(AgentResponse);

    // 查询 agent_response 获取 bot_name
    const agentResponse = await repo.findOneBy({ session_id });
    const botName = agentResponse?.bot_name;

    // 设置 bot context
    const contextData = context.createContext(botName || undefined, undefined, payload.lane);

    await context.run(contextData, async () => {
        if (status === 'failed') {
            console.error(
                `[ChatResponseWorker] Agent failed: session_id=${session_id}, error=${error}`,
            );
            await repo.update({ session_id }, { status: 'failed' });
            rabbitmqClient.ack(msg);
            return;
        }

        if (!content) {
            console.warn(`[ChatResponseWorker] Empty content: session_id=${session_id}, part=${part_index}`);
            if (is_last) {
                await repo.update({ session_id }, { status: 'completed' });
            }
            rabbitmqClient.ack(msg);
            return;
        }

        try {
            // 群聊中将 @用户名 替换为 <at union_id="xxx">用户名</at>
            const resolvedContent = is_p2p
                ? content
                : await resolveMentionsForGroup(content, chat_id);
            const postContent = markdownToPostContent(resolvedContent);

            // 发送消息并捕获 AI 消息 ID
            let aiMessageId: string | undefined;
            if (part_index === 0) {
                // 第一条作为回复
                aiMessageId = await replyPost(message_id, postContent);
            } else {
                // 后续消息带延迟后发送
                await sleep(SEND_DELAY_MS);
                aiMessageId = await sendPost(chat_id, postContent);
            }

            const effectiveMessageId = aiMessageId || `${message_id}_part${part_index}`;

            // 每条消息发完后立即存 conversation_messages
            const now = dayjs().valueOf();
            await storeMessage({
                user_id: getBotUnionId(),
                content: MessageContentUtils.wrapMarkdownAsV2(content),
                role: 'assistant',
                message_id: effectiveMessageId,
                message_type: 'post',
                chat_id: chat_id,
                chat_type: is_p2p ? 'p2p' : 'group',
                create_time: String(now),
                root_message_id: root_id || message_id,
                reply_message_id: message_id,
            });

            // 每条消息追加到 agent_responses.replies jsonb 数组
            const replyEntry = JSON.stringify([
                {
                    message_id: effectiveMessageId,
                    content_type: 'post',
                    sent_at: new Date().toISOString(),
                },
            ]);
            await repo
                .createQueryBuilder()
                .update(AgentResponse)
                .set({
                    replies: () =>
                        `COALESCE(replies, '[]'::jsonb) || '${replyEntry}'::jsonb`,
                })
                .where('session_id = :sid', { sid: session_id })
                .execute();

            // is_last 时更新 response_text 和状态
            if (is_last) {
                await repo.update(
                    { session_id },
                    {
                        response_text: full_content || content,
                        status: 'completed',
                    },
                );
            }

            console.info(`[ChatResponseWorker] Reply sent: session_id=${session_id}, part=${part_index}, ai_msg_id=${effectiveMessageId}`);
        } catch (e) {
            console.error(`[ChatResponseWorker] Failed to send reply: session_id=${session_id}, part=${part_index}`, e);
            await repo.update({ session_id }, { status: 'failed' });
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
    const queue = laneQueue(QUEUE_CHAT_RESPONSE, lane);
    await rabbitmqClient.consume(queue, handleChatResponse);
    console.info(
        `[ChatResponseWorker] Consuming queue: ${queue}, waiting for messages...`,
    );
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
