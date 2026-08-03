/**
 * Recall Worker — 独立进程
 *
 * 消费 RabbitMQ recall queue，根据 session_id 查找 common_agent_response，
 * 调用对应 channel 插件撤回消息，更新 safety_status。
 *
 * 模块结构：handleRecall（纯业务、依赖注入）+ main（进程装配）。进程级副作用
 * （文件日志 / console 接管、DB 连接、MQ 连接、信号处理）全部收在 main 与
 * import.meta.main 守卫里，模块本身 import 安全，测试可以直接喂 payload + header
 * 跑整条 handleRecall。
 */

import AppDataSource from 'ormconfig';
import { LoggerFactory } from '@inner/shared';
import { CommonAgentResponse } from '@inner/shared/entities';
import {
    rabbitmqClient,
    RECALL,
    getLane,
    laneQueue,
} from '@inner/shared/mq';
import { laneFromMessage, traceIdFromMessage } from '@inner/shared/mq-context';
import { botDirectory } from '@inner/shared/bot';
import { context } from '@middleware/context';
import { getChannelRegistry } from '@inner/shared/channel';
import '@plugins/index';
import { initializeChannelPlugins } from '@plugins/initialize';
import { recallReplies } from './recall-outbound';
import type { Repository } from 'typeorm';
import type { OutboundCapabilities } from '@inner/shared/channel';
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
    // 上游仍在 body 里带 lane，但**判 lane 不看这里**：lane 只认 AMQP header，
    // 口径见 @inner/shared/mq-context 的 laneFromMessage。
    lane?: string;
}

// handler 的可注入依赖。main 灌真实实现，测试灌 spy。
export interface RecallHandlerDeps {
    repo: Repository<CommonAgentResponse>;
    getCapabilities: (channel: string) => OutboundCapabilities;
    // replies 还没落库时延时重投一条 recall。lane 由 handler 按入站 header 决定，
    // 'prod' 表示投回 prod 队列。
    republish: (
        payload: Record<string, unknown>,
        delayMs: number,
        headers: Record<string, unknown>,
        lane: string,
    ) => Promise<void>;
    ack: (msg: ConsumeMessage) => void;
    nack: (msg: ConsumeMessage, requeue?: boolean) => void;
}

export async function handleRecall(deps: RecallHandlerDeps, msg: ConsumeMessage): Promise<void> {
    // 整条处理都跑在入站消息的上下文里：lane 和 trace_id 都从 AMQP header 恢复
    // （口径见 @inner/shared/mq-context）。
    //
    // 为什么必须是整条、而不是只包住撤回那一段：重投走 rabbitmq.ts::publish，而
    // publish 的 trace_id 取自 AsyncLocalStorage —— 重投分支跑在 context 之外时写进
    // header 的就是空串，真实重试路径上 trace 链断掉。显式往 headers 里塞 trace_id
    // 也没用：publish 内部的权威值会盖掉调用方给的。
    //
    // bot_name 要查库才知道，撤回前再开一层带 botName 的内层 context（沿用同一个
    // traceId / lane）。
    const lane = laneFromMessage(msg);
    // 入站没带 trace_id 时 createContext 生成一个，内层复用它这个**生成后**的值，
    // 否则内外两层会是两条不同的 trace。
    const inbound = context.createContext(undefined, traceIdFromMessage(msg), lane);
    return context.run(inbound, () => runRecall(deps, msg, lane, inbound.traceId));
}

async function runRecall(
    deps: RecallHandlerDeps,
    msg: ConsumeMessage,
    lane: string | undefined,
    traceId: string,
): Promise<void> {
    const { repo, getCapabilities, republish, ack, nack } = deps;

    const payload: RecallPayload = JSON.parse(msg.content.toString());
    const { session_id, reason, detail, channel = DEFAULT_CHANNEL } = payload;

    console.info(
        `[RecallWorker] Processing recall: session_id=${session_id}, channel=${channel}, lane=${lane ?? 'prod'}, reason=${reason}`,
    );

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
        ack(msg);
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
            // 重投沿用入站 header 解析出的 lane：目标队列要回原泳道，没有 lane 就
            // **显式**投回 prod——lane 参数传 undefined 会让 publish 回落进程 env
            // LANE，prod worker 接手降级消息时那正是要命的误判。
            //
            // 随行 header 只写重试计数：lane header 由 publish 按上面这个 lane 参数
            // 统一注入（连同 trace_id），调用点不重复写一份。
            await republish(
                payload as unknown as Record<string, unknown>,
                delayMs,
                { 'x-retry-count': retryCount + 1 },
                lane ?? 'prod',
            );
            ack(msg);
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
        nack(msg, false);
        return;
    }

    // 设置 bot context 以使用正确的 Lark client；traceId / lane 沿用入站那条，
    // 撤回打出去的平台请求和上面的日志才在同一条 trace 上。
    const botName = agentResponse.bot_name;
    const contextData = context.createContext(botName || undefined, traceId, lane);
    let recalledCount = 0;
    let failedCount = 0;

    await context.run(contextData, async () => {
        // 逐条撤回走渠道能力端口。common_message_id → 渠道裸 message id 的反查
        // 在插件内完成，worker 不碰任何平台私有映射表。
        const capabilities = getCapabilities(channel);
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

    ack(msg);
    console.info(`[RecallWorker] Recall completed: session_id=${session_id}`);
}

async function main(): Promise<void> {
    // 文件日志 + console 接管：进程入口独占的副作用。ESM 的 import 本来就在模块
    // 体之前求值，放这里和放模块顶层的实际时机一致，但模块变成 import 安全的。
    LoggerFactory.createLogger({
        enableFileLogging: true,
        logDir: process.env.LOG_DIR || '/var/log/channel-server',
        logFileName: 'recall-worker.log',
        enableConsoleOverride: true,
    });

    console.info('[RecallWorker] Starting...');

    // 1. 初始化数据库
    await AppDataSource.initialize();
    console.info('[RecallWorker] Database connected');

    // 2. 初始化 channel 插件客户端
    await botDirectory.load();
    await initializeChannelPlugins();
    console.info('[RecallWorker] Channel plugins initialized');

    // 3. 连接 RabbitMQ 并声明拓扑
    await rabbitmqClient.connect();
    await rabbitmqClient.declareTopology();
    console.info('[RecallWorker] RabbitMQ connected');

    // 4. 开始消费（按泳道）
    const deps: RecallHandlerDeps = {
        repo: AppDataSource.getRepository(CommonAgentResponse),
        getCapabilities: (channel) => getChannelRegistry().get(channel).capabilities,
        republish: (payload, delayMs, headers, lane) =>
            rabbitmqClient.publish(RECALL, payload, delayMs, headers, lane),
        ack: (msg) => rabbitmqClient.ack(msg),
        nack: (msg, requeue) => rabbitmqClient.nack(msg, requeue),
    };

    const lane = getLane();
    const queue = laneQueue(RECALL.queue, lane);
    await rabbitmqClient.consume(queue, (msg) => handleRecall(deps, msg));
    console.info(`[RecallWorker] Consuming queue: ${queue}, waiting for messages...`);
}

// 只有作为进程入口（bun src/workers/recall-worker.ts / 编译产物 recall-worker）
// 时才启动；被 import（测试）时模块零副作用。
if (import.meta.main) {
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
}
