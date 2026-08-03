// 交接的投递侧：这条消息不归本泳道，交给它真正的主人。
//
// lane-queue.ts 是同一件事的另一头（接住别人交过来的）。信封的形状、队列名、去重
// key 都定义在那里 —— 两侧共用一份定义，改了字段名不会只改一半。
//
// 判断和投递刻意分开：
//   chooseInboundLane      纯决策，四个分支，不碰任何 I/O
//   handOffToInboundLane   只管写进队列，不参与决策
// 混在一起的话，"绑定改了之后消息会不会被二次转投"这种问题就只能靠连着 MQ 才能验。
//
// fail-closed：投递失败一律往上抛。绝不"投不出去就退回本地处理" —— 本地处理的是
// 泳道那份改动之外的代码，等于拿线上代码跑了一条本该在泳道验证的消息，而且没有
// 任何信号。

import { DynamicConfig } from '@inner/shared';
import { getRabbitConfirmChannel } from '@inner/shared/mq';

import { inboundLaneQueueName, type InboundLaneEnvelope } from './lane-queue';

// ---- 判断 ----

export interface InboundLaneChoice {
    /** true = 交给 lane 那条泳道，本进程到此为止。 */
    handOff: boolean;
    lane: string;
}

export interface InboundLaneInput {
    /** 动态开关。关着的时候连算都不算（也就不打 DB），行为与开关引入之前逐字节一致。 */
    dispatchEnabled: boolean;
    /** 本进程所在泳道。prod 部署是 'prod'。 */
    currentLane: string;
    /** 这条消息按绑定该归哪条泳道。只在真的需要判断时才会被调用。 */
    laneOf: () => Promise<string>;
}

export async function chooseInboundLane(input: InboundLaneInput): Promise<InboundLaneChoice> {
    if (!input.dispatchEnabled) {
        return { handOff: false, lane: input.currentLane };
    }

    // 泳道部署手上的消息是 prod 判过一次之后交过来的，信封里的 lane 才是权威。再判
    // 一次的后果很实：绑定在投递之后被改掉时，同一条消息会被二次转投到别的泳道，
    // 甚至投进没有任何消费者的 inbound_lane.prod。
    if (input.currentLane !== 'prod') {
        return { handOff: false, lane: input.currentLane };
    }

    const lane = await input.laneOf();
    // 绝不投给自己：那会让同一条消息在本进程处理两遍。
    if (lane === input.currentLane) {
        return { handOff: false, lane };
    }
    return { handOff: true, lane };
}

// ---- 投递 ----

/**
 * 投递用到的 amqp 表面，就这两个方法 —— 而且 sendToQueue 带**确认回调**，也就是说
 * 这里要的是一条 confirm channel，不是普通 channel。
 */
export interface LaneHandoffChannel {
    assertQueue(queue: string, options: { durable: boolean }): Promise<unknown>;
    sendToQueue(
        queue: string,
        content: Buffer,
        options: { persistent: boolean },
        confirmed: (error: Error | null) => void,
    ): boolean;
}

/**
 * 队列声明是 fail-closed 的：**不配 TTL、不配死信**。
 *
 * 这跟共享 MQ 客户端给普通泳道队列配的那一套（10s TTL + 死信回 prod）刻意不同。
 * 装在这里的是"已经判定该在这条泳道处理"的消息：过期跑回 prod 就是拿泳道里那份
 * 未验证的改动去污染线上；而 inbound_lane 没有 prod 基队列，死信投不出去会直接丢。
 * 消费者不在线时宁可堆积。
 *
 * **必须等 broker 确认**。`persistent: true` 只约束"broker 收到之后要落盘"，不证明
 * broker 收到了 —— 普通 channel 写进本地缓冲就返回 true，连接在那一刻断掉消息就
 * 静默没了。而交接这条路上，投完就 return、本地不留任何账，丢了没有第二次机会：
 * 飞书那侧早就 ACK 过了。
 */
export async function handOffToInboundLane(
    amqp: LaneHandoffChannel,
    envelope: InboundLaneEnvelope,
): Promise<void> {
    const queue = inboundLaneQueueName(envelope.lane);
    await amqp.assertQueue(queue, { durable: true });

    await new Promise<void>((resolve, reject) => {
        amqp.sendToQueue(
            queue,
            Buffer.from(JSON.stringify(envelope)),
            { persistent: true },
            (error) => {
                if (!error) return resolve();
                // 抛出去让调用方走可重试路径，绝不当成功。
                reject(
                    new Error(
                        `broker did not confirm the handoff to ${queue} ` +
                            `(message=${envelope.global_message_id}): ${error.message}`,
                    ),
                );
            },
        );
    });
}

/** 生产装配：取带确认的共享 channel 后投递。 */
export async function handOffOverRabbit(envelope: InboundLaneEnvelope): Promise<void> {
    const amqp = await getRabbitConfirmChannel();
    return handOffToInboundLane(amqp as unknown as LaneHandoffChannel, envelope);
}

// ---- 开关 ----

/**
 * "处理层是否按绑定分流"。默认关 —— 读不到、没配、读失败一律按不分流，与拆分前
 * 一致。key 与 channel-server 用的是同一个：切换期间两个服务必须同进同出，不然
 * 会出现一边分流一边不分流的双跑。
 */
export const INBOUND_LANE_DISPATCH_FLAG = 'enable_inbound_lane_dispatch';

const dynamicConfig = new DynamicConfig();

export function inboundLaneDispatchEnabled(): Promise<boolean> {
    return dynamicConfig.getBool(INBOUND_LANE_DISPATCH_FLAG, false);
}
