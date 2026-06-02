// 入站 lane 分发 MQ（lane-routing-redesign §4）。
//
// 职责：prod channel-server 算出非 prod lane 后，把「已解析的平台无关入站消息」
// 投到 inbound_lane.{lane}；目标 lane 的 channel-server 起消费者只消费自己 lane。
//
// fail-closed（§4.6，致命语义，绝不照搬现状）：inbound_lane.{lane} 装的是「已被
// prod 决策点判定该走这个非 prod lane」的消息。它**绝不**复用现状 rabbitmq.ts 给
// lane 队列默认配的 10s TTL + dead-letter 回 prod —— 那套会让本该在 lane 处理的
// 消息 10s 后跑回 prod（双写双处理污染 prod），或因 inbound_lane 没有 prod base
// 队列而 dead-letter 无处投直接丢。所以这里 lane 消费者缺席时消息**留在队列**等
// 消费者上线，宁可堆积也不偷偷落 prod。
//
// 与现状 MQ 链路正交：下游 chat_request_{lane} / vectorize_{lane} 等 lane 内流水线
// 队列不动，本模块只加 inbound_lane.{lane} 这一类「lane 间投递」队列。

import type { Channel } from 'amqplib';
import { getRabbitChannel } from './rabbitmq';

// 投到 inbound_lane.{lane} 的消息信封：平台无关，带分流三要素 + 原始事件 params。
// lane 写进信封（不是 HTTP header，跨 lane 是 MQ），lane channel-server 消费时从
// 信封读出 lane 注入 context（§6）。
export interface InboundLaneEnvelope {
    // 目标 channel。旧队列信封可能没有该字段，消费侧按 lark 兼容。
    channel?: string;
    event_type: string;
    global_message_id: string;
    // 当前请求 traceId。跨 lane 走 MQ 时不能靠 HTTP header 透传，必须写进信封，
    // lane 消费侧据此重建 context，保持端到端日志可关联。
    trace_id: string;
    lane: string;
    // 投递这条消息的 bot 名。跨 lane 走 MQ，botName 不能像现状那样靠 HTTP
    // X-App-Name header 传，必须写进信封——lane channel-server 消费时据此注入
    // context.botName，否则入站后半段（handleMessageReceive 读 context.getBotName()）
    // 拿不到 bot 身份。
    bot_name: string;
    // 原始飞书事件 params（lane channel-server 走入站后半段时复用）。平台无关层
    // 不解释它的内容，只透传给目标 lane 的同一套入站处理。
    params: unknown;
}

const QUEUE_PREFIX = 'inbound_lane';

export function inboundLaneQueueName(lane: string): string {
    return `${QUEUE_PREFIX}.${lane}`;
}

// 三元组幂等 key（§4.4 point 5）：event_type + globalMessageId + lane 唯一确定一次
// 入站处理。消费侧据此去重，重复投递的同一三元组直接跳过整条入站处理。
export function inboundDedupeKey(env: InboundLaneEnvelope): string {
    return `${QUEUE_PREFIX}:${env.event_type}:${env.global_message_id}:${env.lane}`;
}

// fail-closed 队列声明：只 durable，**不设** x-message-ttl、**不设** dead-letter。
// 与 rabbitmq.ts 的 lane 队列（带 10s TTL + DLX 回 prod）刻意不同。
export async function assertInboundLaneQueue(ch: Channel, lane: string): Promise<void> {
    await ch.assertQueue(inboundLaneQueueName(lane), { durable: true });
}

// 投递：声明 + 发送，任一步失败直接抛错（fail-closed，调用方记错误日志/告警，
// 绝不静默回 prod）。
export async function publishInboundLane(
    ch: Channel,
    env: InboundLaneEnvelope,
): Promise<void> {
    await assertInboundLaneQueue(ch, env.lane);
    ch.sendToQueue(
        inboundLaneQueueName(env.lane),
        Buffer.from(JSON.stringify(env)),
        { persistent: true },
    );
}

// 生产侧便捷入口：取共享 channel 后投递。决策点（handlers）调这个。
export async function dispatchToInboundLane(env: InboundLaneEnvelope): Promise<void> {
    const ch = getRabbitChannel();
    await publishInboundLane(ch, env);
}
