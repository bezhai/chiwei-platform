/**
 * Recall Worker — 独立进程
 *
 * 消费 RabbitMQ recall queue，根据 session_id 查找 common_agent_response，
 * 调用对应 channel 插件撤回消息，更新 safety_status。
 */


import { LoggerFactory } from '@inner/shared';

// Initialize file logging before any other imports that use console.*
LoggerFactory.createLogger({
    enableFileLogging: true,
    logDir: process.env.LOG_DIR || '/var/log/channel-server',
    logFileName: 'recall-worker.log',
    enableConsoleOverride: true,
});

import AppDataSource from 'ormconfig';
import { CommonAgentResponse } from '@entities/common-agent-response';
import {
    rabbitmqClient,
    RECALL,
    getLane,
    laneQueue,
} from '@integrations/rabbitmq';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { context } from '@middleware/context';
import { getChannelRegistry } from '@core/registry/channel-registry';
import '@plugins/index';
import { initializeChannelPlugins } from '@plugins/initialize';
import { recallReplies } from './recall-outbound';
import type { ConsumeMessage } from 'amqplib';

// 撤回走渠道能力端口：worker 只按 payload.channel 取插件，common id 反查和
// 平台 delete/recall 都由当前 channel 的 capabilities 完成。旧 payload 不带
// channel 时仍按 lark 处理。
const DEFAULT_CHANNEL = 'lark';

const MAX_RETRY = 3;
const RETRY_DELAYS = [5000, 10000, 15000];

interface RecallPayload {
    channel?: string;
    session_id: string;
    chat_id?: string;
    trigger_message_id?: string;
    reason: string;
    detail?: string;
    lane?: string;
}

async function handleRecall(msg: ConsumeMessage): Promise<void> {
    const payload: RecallPayload = JSON.parse(msg.content.toString());
    const { session_id, reason, detail, channel = DEFAULT_CHANNEL } = payload;

    console.info(`[RecallWorker] Processing recall: session_id=${session_id}, channel=${channel}, reason=${reason}`);

    const repo = AppDataSource.getRepository(CommonAgentResponse);
    const agentResponse = await repo.findOneBy({ session_id });

    // Phase 2: 终态短路，防止重复 Recall 把 recalled 覆盖成 recall_failed。
    // run_post_safety 的 TERMINAL_STATUSES short-circuit 假设 recall-worker
    // 不会改写终态；这里对称做一次入口检查。
    if (
        agentResponse?.safety_status === 'recalled' ||
        agentResponse?.safety_status === 'recall_failed'
    ) {
        console.info(
            `[RecallWorker] short-circuit: session_id=${session_id} already ${agentResponse.safety_status}`,
        );
        rabbitmqClient.ack(msg);
        return;
    }

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
                RECALL,
                payload as unknown as Record<string, unknown>,
                delayMs,
                { 'x-retry-count': retryCount + 1 },
                payload.lane,
            );
            rabbitmqClient.ack(msg);
            return;
        }
        // 达到最大重试次数：在进 DLQ 之前写 recall_failed 终态，
        // 避免新链路下 status 永远停在 pending（Phase 2 §4.4）
        console.error(
            `[RecallWorker] Max retries reached for session_id=${session_id}, marking recall_failed and sending to DLQ`,
        );
        try {
            await repo.update(
                { session_id },
                {
                    safety_status: 'recall_failed',
                    safety_result: {
                        reason,
                        detail,
                        recalled: 0,
                        failed: 0,
                        checked_at: new Date().toISOString(),
                    },
                },
            );
        } catch (e) {
            console.error(`[RecallWorker] Failed to write recall_failed status:`, e);
        }
        rabbitmqClient.nack(msg, false);
        return;
    }

    // 设置 bot context 以使用正确的 Lark client
    const botName = agentResponse.bot_name;
    const contextData = context.createContext(botName || undefined, undefined, payload.lane);
    let recalledCount = 0;
    let failedCount = 0;

    await context.run(contextData, async () => {
        // 逐条撤回走渠道能力端口。common_message_id → 渠道裸 message id 的反查
        // 在插件内完成，worker 不碰任何平台私有映射表。
        const capabilities = getChannelRegistry().get(channel).capabilities;
        const result = await recallReplies(capabilities, agentResponse.replies);
        recalledCount = result.recalled;
        failedCount = result.failed;
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

    // 2. 初始化 channel 插件客户端
    await multiBotManager.initialize();
    await initializeChannelPlugins();
    console.info('[RecallWorker] Channel plugins initialized');

    // 3. 连接 RabbitMQ 并声明拓扑
    await rabbitmqClient.connect();
    await rabbitmqClient.declareTopology();
    console.info('[RecallWorker] RabbitMQ connected');

    // 4. 开始消费（按泳道）
    const lane = getLane();
    const queue = laneQueue(RECALL.queue, lane);
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
