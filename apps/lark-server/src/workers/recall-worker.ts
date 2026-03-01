/**
 * Recall Worker — 独立进程
 *
 * 消费 RabbitMQ recall queue，根据 session_id 查找 agent_responses，
 * 调用飞书 deleteMessage 撤回消息，更新 safety_status。
 */


import { LoggerFactory } from '@inner/shared';

// Initialize file logging before any other imports that use console.*
LoggerFactory.createLogger({
    enableFileLogging: true,
    logDir: process.env.LOG_DIR || '/var/log/lark-server',
    logFileName: 'recall-worker.log',
    enableConsoleOverride: true,
});

import AppDataSource from 'ormconfig';
import { AgentResponse } from '@entities/agent-response';
import {
    rabbitmqClient,
    RK_RECALL,
    QUEUE_RECALL,
    getLane,
    laneQueue,
} from '@integrations/rabbitmq';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { initializeLarkClients } from '@integrations/lark-client';
import { deleteMessage } from '@lark-client';
import { context } from '@middleware/context';
import type { ConsumeMessage } from 'amqplib';

const MAX_RETRY = 3;
const RETRY_DELAYS = [5000, 10000, 15000];

interface RecallPayload {
    session_id: string;
    chat_id?: string;
    trigger_message_id?: string;
    reason: string;
    detail?: string;
    lane?: string;
}

async function handleRecall(msg: ConsumeMessage): Promise<void> {
    const payload: RecallPayload = JSON.parse(msg.content.toString());
    const { session_id, reason, detail } = payload;

    console.info(`[RecallWorker] Processing recall: session_id=${session_id}, reason=${reason}`);

    const repo = AppDataSource.getRepository(AgentResponse);
    const agentResponse = await repo.findOneBy({ session_id });

    if (!agentResponse || agentResponse.replies.length === 0) {
        // replies 还未保存（race condition），延时重投
        const retryCount = (msg.properties.headers?.['x-retry-count'] as number) || 0;
        if (retryCount < MAX_RETRY) {
            const delayMs = RETRY_DELAYS[retryCount] || 15000;
            console.warn(
                `[RecallWorker] No replies yet for session_id=${session_id}, ` +
                    `retrying (${retryCount + 1}/${MAX_RETRY}) with delay ${delayMs}ms`,
            );
            await rabbitmqClient.publish(
                RK_RECALL,
                payload as unknown as Record<string, unknown>,
                delayMs,
                { 'x-retry-count': retryCount + 1 },
                payload.lane,
            );
            rabbitmqClient.ack(msg);
            return;
        }
        // 达到最大重试次数，nack → DLQ
        console.error(
            `[RecallWorker] Max retries reached for session_id=${session_id}, sending to DLQ`,
        );
        rabbitmqClient.nack(msg, false);
        return;
    }

    // 设置 bot context 以使用正确的 Lark client
    const botName = agentResponse.bot_name;
    const contextData = context.createContext(botName || undefined);
    let recalledCount = 0;
    let failedCount = 0;

    await context.run(contextData, async () => {
        // 逐条撤回
        for (const reply of agentResponse.replies) {
            try {
                await deleteMessage(reply.message_id);
                recalledCount++;
                console.info(`[RecallWorker] Recalled message: ${reply.message_id}`);
            } catch (e) {
                failedCount++;
                console.error(`[RecallWorker] Failed to recall message: ${reply.message_id}`, e);
            }
        }
    });

    // 仅当实际撤回了消息才标记为 recalled
    const status = recalledCount > 0 ? 'recalled' : 'recall_failed';
    await repo.update(
        { session_id },
        {
            safety_status: status,
            safety_result: {
                reason,
                detail,
                recalled: recalledCount,
                failed: failedCount,
                checked_at: new Date().toISOString(),
            },
        },
    );

    if (failedCount > 0) {
        console.error(
            `[RecallWorker] Partial failure: session_id=${session_id}, ` +
                `recalled=${recalledCount}, failed=${failedCount}`,
        );
    }

    rabbitmqClient.ack(msg);
    console.info(`[RecallWorker] Recall completed: session_id=${session_id}`);
}

async function main(): Promise<void> {
    console.info('[RecallWorker] Starting...');

    // 1. 初始化数据库
    await AppDataSource.initialize();
    console.info('[RecallWorker] Database connected');

    // 2. 初始化 Lark 客户端（用于 deleteMessage）
    await multiBotManager.initialize();
    await initializeLarkClients();
    console.info('[RecallWorker] Lark clients initialized');

    // 3. 连接 RabbitMQ 并声明拓扑
    await rabbitmqClient.connect();
    await rabbitmqClient.declareTopology();
    console.info('[RecallWorker] RabbitMQ connected');

    // 4. 开始消费（按泳道）
    const lane = getLane();
    const queue = laneQueue(QUEUE_RECALL, lane);
    await rabbitmqClient.consume(queue, handleRecall);
    console.info(`[RecallWorker] Consuming queue: ${queue}, waiting for messages...`);
}

main().catch((err) => {
    console.error('[RecallWorker] Fatal error:', err);
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
