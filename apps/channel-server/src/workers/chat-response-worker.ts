/**
 * Chat Response Worker — 独立进程
 *
 * 消费 RabbitMQ chat_response queue，
 * 按 part_index 直接发送 post 消息到对应 channel，
 * 每条消息发送后立即存 common_message/channel 私有映射并追加 common_agent_response.replies，
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
import { getChannelRegistry } from '@core/registry/channel-registry';
import '@plugins/index';
import { initializeChannelPlugins } from '@plugins/initialize';
import {
    handleChatResponse,
    type ChatResponseHandlerDeps,
} from './chat-response-handler';
import { Histogram, Registry, collectDefaultMetrics } from 'prom-client';
import type { ConsumeMessage } from 'amqplib';

// Metrics (chat-response-worker is a standalone process, needs its own registry)
const metricsRegistry = new Registry();
collectDefaultMetrics({ register: metricsRegistry });

const chatResponseDuration = new Histogram({
    name: 'chat_response_duration_seconds',
    help: 'Duration of each chat-response-worker stage',
    labelNames: ['stage'] as const,  // db_query, resolve, channel_send, db_write, total
    buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5],
    registers: [metricsRegistry],
});

const chatResponseQueueDelay = new Histogram({
    name: 'chat_response_queue_delay_seconds',
    help: 'Time spent waiting in MQ queue (chat_response)',
    buckets: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
    registers: [metricsRegistry],
});

// 把进程级真实依赖（DB repo / MQ ack-nack / 渠道插件 / metrics）灌进 handler。
// 消息处理的全部业务逻辑在 chat-response-handler.ts，本入口只做装配。
function buildHandlerDeps(): ChatResponseHandlerDeps {
    return {
        repo: AppDataSource.getRepository(CommonAgentResponse),
        getCapabilities: (channel) => getChannelRegistry().get(channel).capabilities,
        ack: (msg) => rabbitmqClient.ack(msg),
        nack: (msg, requeue) => rabbitmqClient.nack(msg, requeue),
        observeDuration: (stage, seconds) =>
            chatResponseDuration.labels({ stage }).observe(seconds),
        observeQueueDelay: (seconds) => chatResponseQueueDelay.observe(seconds),
    };
}

async function main(): Promise<void> {
    console.info('[ChatResponseWorker] Starting...');

    // 1. 初始化数据库
    await AppDataSource.initialize();
    console.info('[ChatResponseWorker] Database connected');

    // 2. 初始化 channel 插件客户端
    await multiBotManager.initialize();
    await initializeChannelPlugins();
    console.info('[ChatResponseWorker] Channel plugins initialized');

    // 3. 连接 RabbitMQ 并声明拓扑
    await rabbitmqClient.connect();
    await rabbitmqClient.declareTopology();
    console.info('[ChatResponseWorker] RabbitMQ connected');

    // 4. 开始消费
    const lane = getLane();
    const queue = laneQueue(CHAT_RESPONSE.queue, lane);
    const deps = buildHandlerDeps();
    await rabbitmqClient.consume(queue, (msg) => handleChatResponse(deps, msg));
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
