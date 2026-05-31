// handlers 决策点的组装函数（lane-routing-redesign §3.1/§4）。把「读 flag → 算
// lane → 非本进程 lane 投 inbound_lane.{lane}」串起来，让 handleMessageReceive
// 只需一行调用：返回 true 表示已分流到别的 lane（handler 应立即 return，本地不再
// 处理）；返回 false 表示本地继续走现状入站链路。
//
// 分叉决策本身是纯函数 resolveInboundDispatch（已测）；本函数只做装配：注入真实
// flag（isInboundLaneDispatchEnabled，default off）+ 真实 resolveLane
// （getLaneRouter）+ 真实投递（dispatchToInboundLane，fail-closed）。
//
// 零回归红线：flag off 时 resolveInboundDispatch 直接返回 local 且不调 resolveLane，
// 本函数 publish 一次都不发——行为与现状逐字节一致。

import { resolveInboundDispatch } from './inbound-lane-decision';
import { dispatchToInboundLane } from './inbound-lane';
import { isInboundLaneDispatchEnabled } from './inbound-lane-flag';
import { getLaneRouter } from './lane-router-runtime';

export interface InboundDispatchContext {
    // 本进程所属 lane（prod channel-server = 'prod'，由 rabbitmq.getLane() 取，
    // 这里要求调用方传 'prod' 或具体 lane，不传 undefined）。
    currentLane: string;
    channel: string;
    botGlobalId: string;
    eventType: string;
    globalMessageId: string;
    traceId: string;
    // 原始平台事件 params，透传进信封供目标 lane channel-server 重走入站。
    params: unknown;
}

// 返回 true = 已投到别的 lane，handler 应 return；false = 本地继续处理。
export async function dispatchInboundIfNeeded(
    ctx: InboundDispatchContext,
): Promise<boolean> {
    const flagEnabled = await isInboundLaneDispatchEnabled();
    const decision = await resolveInboundDispatch({
        flagEnabled,
        currentLane: ctx.currentLane,
        channel: ctx.channel,
        botGlobalId: ctx.botGlobalId,
        resolveLane: (channel, botGlobalId) =>
            getLaneRouter().resolveLane(channel, botGlobalId),
    });

    if (decision.action === 'local') {
        return false;
    }

    // dispatch：投 inbound_lane.{lane}（fail-closed，失败抛错可观测，绝不静默回 prod）。
    // bot_name = botGlobalId：bot 维度下全局 bot 标识就是 bot_name，lane 消费侧据此
    // 注入 context.botName。
    await dispatchToInboundLane({
        event_type: ctx.eventType,
        global_message_id: ctx.globalMessageId,
        trace_id: ctx.traceId,
        lane: decision.lane,
        bot_name: ctx.botGlobalId,
        params: ctx.params,
    });
    console.info(
        `[inbound-lane] dispatched to lane=${decision.lane} ` +
            `event=${ctx.eventType} gmid=${ctx.globalMessageId}`,
    );
    return true;
}
