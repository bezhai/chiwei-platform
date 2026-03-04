/**
 * Chat Response Worker — 独立进程
 *
 * 消费 RabbitMQ chat_response queue，
 * 使用 TextReplyStrategy 发送 post 消息到飞书，
 * 更新 agent_responses 状态并保存 conversation_messages。
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
import { TextReplyStrategy } from '@core/services/ai/strategies/text-reply.strategy';
import { multiMessageConfig } from '@config/multi-message.config';
import { getBotUnionId } from '@core/services/bot/bot-var';
import { MessageContentUtils } from 'core/models/message-content';
import dayjs from 'dayjs';
import type { ConsumeMessage } from 'amqplib';

interface ChatResponsePayload {
    session_id: string;
    message_id: string;
    chat_id: string;
    is_p2p: boolean;
    root_id?: string;
    user_id?: string;
    content: string;
    status: 'success' | 'failed';
    error?: string;
    lane?: string;
}

async function handleChatResponse(msg: ConsumeMessage): Promise<void> {
    const payload: ChatResponsePayload = JSON.parse(msg.content.toString());
    const { session_id, message_id, chat_id, is_p2p, root_id, content, status, error } = payload;

    console.info(
        `[ChatResponseWorker] Processing: session_id=${session_id}, status=${status}`,
    );

    const repo = AppDataSource.getRepository(AgentResponse);

    // 查询 agent_response 获取 bot_name
    const agentResponse = await repo.findOneBy({ session_id });
    const botName = agentResponse?.bot_name;

    // 设置 bot context
    const contextData = context.createContext(botName || undefined);

    await context.run(contextData, async () => {
        if (status === 'failed') {
            console.error(
                `[ChatResponseWorker] Agent failed: session_id=${session_id}, error=${error}`,
            );
            // 更新状态为 failed
            await repo.update({ session_id }, { status: 'failed' });
            rabbitmqClient.ack(msg);
            return;
        }

        if (!content) {
            console.warn(`[ChatResponseWorker] Empty content: session_id=${session_id}`);
            await repo.update({ session_id }, { status: 'completed' });
            rabbitmqClient.ack(msg);
            return;
        }

        try {
            // 使用 TextReplyStrategy 发送消息
            const strategy = new TextReplyStrategy(
                { messageId: message_id, chatId: chat_id, isP2P: is_p2p, rootId: root_id },
                multiMessageConfig,
            );
            await strategy.sendReply(content);

            // 更新 agent_responses
            await repo.update(
                { session_id },
                {
                    response_text: content,
                    replies: [
                        {
                            message_id: message_id,
                            content_type: 'post',
                            sent_at: new Date().toISOString(),
                        },
                    ] as any,
                    status: 'completed',
                },
            );

            // 保存 assistant 消息到 conversation_messages
            const now = dayjs().valueOf();
            await storeMessage({
                user_id: getBotUnionId(),
                content: MessageContentUtils.wrapMarkdownAsV2(content),
                role: 'assistant',
                message_id: message_id,
                message_type: 'post',
                chat_id: chat_id,
                chat_type: is_p2p ? 'p2p' : 'group',
                create_time: String(now),
                root_message_id: root_id,
                reply_message_id: message_id,
            });

            console.info(`[ChatResponseWorker] Reply sent: session_id=${session_id}`);
        } catch (e) {
            console.error(`[ChatResponseWorker] Failed to send reply: session_id=${session_id}`, e);
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
