// 入站 lane 消费者（lane-routing-redesign §4.3/§4.4）。lane channel-server 起这个
// 消费者，**只**消费自己 lane 的 inbound_lane.{LANE}。当前重放完整入站链
// （含 MessageTransferer、识图管线、common bot presence upsert 等前置副作用）；
// 抽出真正后半段是已知待办，本文件暂不改变行为。
//
// 队列声明走 fail-closed 的 assertInboundLaneQueue（无 10s TTL、无 DLX 回 prod，§4.6）。
//
// 三元组幂等（§4.4 point 5）：event_type + globalMessageId + lane 命中已完成 → 跳过
// 整条入站处理（MQ at-least-once 重投不重复处理 / 回复 / 触发副作用）。完成标记只在
// 入站处理成功后写入，失败重投仍会重新处理。

import { getRabbitChannel } from './rabbitmq';
import {
    inboundLaneQueueName,
    assertInboundLaneQueue,
    inboundDedupeKey,
    type InboundLaneEnvelope,
} from './inbound-lane';
import { exists, setNx } from '@cache/redis-client';
import { context } from '@middleware/context';

// 三元组幂等标记 TTL：足够长以覆盖 MQ 重投窗口（远超现状 60s make_reply 锁，因为
// MQ 重投可能晚于回复锁过期，§4.4 point 5 明确点名这个盲区）。
const DEDUPE_TTL_SECONDS = 24 * 60 * 60;

export interface ConsumeDeps {
    // 三元组是否已成功处理。
    isProcessed: (key: string) => Promise<boolean>;
    // 处理成功后写完成标记。
    markProcessed: (key: string) => Promise<void>;
    // 入站处理。
    process: (env: InboundLaneEnvelope) => Promise<void>;
}

// 纯逻辑：可注入 isProcessed/markProcessed/process，确定性测幂等分叉。
export async function consumeInboundLaneEnvelope(
    env: InboundLaneEnvelope,
    deps: ConsumeDeps,
): Promise<void> {
    const key = inboundDedupeKey(env);
    if (await deps.isProcessed(key)) {
        console.info(
            `[inbound-lane] duplicate envelope skipped (already processed): ` +
                `${key}`,
        );
        return;
    }
    await deps.process(env);
    await deps.markProcessed(key);
}

// 生产装配：起 fail-closed 队列消费者，三元组幂等 + 重放入站处理。
// handleMessage 由调用方注入，避免本模块直接 import 飞书 handlers 把 ORM/SDK 拉进来。
export async function startInboundLaneConsumer(
    lane: string,
    handleMessage: (params: unknown) => Promise<void>,
): Promise<void> {
    const ch = getRabbitChannel();
    await assertInboundLaneQueue(ch, lane);
    const queue = inboundLaneQueueName(lane);
    await ch.prefetch(1);
    await ch.consume(queue, async (msg) => {
        if (!msg) return;
        try {
            const env = JSON.parse(msg.content.toString()) as InboundLaneEnvelope;
            await consumeInboundLaneEnvelope(env, {
                isProcessed: async (key) => (await exists(key)) > 0,
                markProcessed: async (key) => {
                    await setNx(key, '1', DEDUPE_TTL_SECONDS);
                },
                process: async (e) => {
                    // 从信封读出 bot_name + lane 注入 context（§6：跨 lane 用信封不用
                    // header）。botName 必须注入，否则入站处理 context.getBotName()
                    // 拿不到 bot 身份。然后走与现状一致的完整入站链；本进程
                    // lane==信封 lane，决策点会判 local，不会再次 dispatch（无自投循环）。
                    await context.run(
                        context.createContext(e.bot_name, e.trace_id, e.lane),
                        async () => {
                            await handleMessage(e.params);
                        },
                    );
                },
            });
            ch.ack(msg);
        } catch (err) {
            // 处理失败不写完成态，并 requeue 交给 MQ redeliver，避免 at-least-once
            // 消息因瞬时错误或进程中断被永久吞掉；仍不 dead-letter 回 prod（§4.6）。
            console.error(`[inbound-lane] consume ${queue} error:`, err);
            ch.nack(msg, false, true);
        }
    });
    console.info(`[inbound-lane] consuming ${queue} (lane=${lane})`);
}
