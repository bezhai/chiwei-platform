// 交接的投递侧：这条消息不归本泳道，交给它真正的主人。两步：
//
//   resolveInboundLaneHandoff  读 flag → 算 lane → 该交出去就备好信封，否则 null
//   handOffToLane              把信封 POST 给目标泳道的 channel-server
//
// 分成两步是因为它们必须落在投影锁的两侧。算 lane 要用投影产出的
// commonConversationId，所以判定跑在锁里；投递是一次同步等对端处理完的跨进程调用，压在
// 锁里就是持锁等外部返回 —— 而那把锁是 Redis 的、prod 与泳道进程共用同一个
// （见 plugins/qq/common-projector.ts）。接收端重走投影会去抢同一个 qq_message_id 的
// 锁，两边互等到窗口超时。
//
// 分叉决策本身是纯函数 resolveInboundDispatch（已测）；这里只做装配：注入真实
// flag（isInboundLaneDispatchEnabled，default off）+ 真实 resolveLane
// （getLaneBindingResolver）+ 真实投递（HTTP，fail-loud）。
//
// 零回归红线：flag off 时 resolveInboundDispatch 直接返回 local 且不调 resolveLane，
// 本模块一个请求都不发——行为与现状逐字节一致。
//
// ## 为什么是 HTTP
//
// 交接曾经是投一条带泳道后缀的队列：泳道没起消费者就没人消费，于是泳道必须陪部入口
// 服务，绑定了已下线的泳道还会让消息静默堆积、bot 变砖。改成带 x-ctx-lane 的内部 HTTP
// 之后，目标由 lane-sidecar 透明解析：泳道的 Service 不在 lite-registry 里时它把请求
// 原样打回 prod，消息以泳道的 context 在 prod 处理，下游泳道队列照常生效。
//
// 代价是没有 MQ 的缓冲和重投：投递失败就是这条消息永远没人处理。所以失败一律 fail-loud
// 上抛，且成功也要能看出到底是送进了泳道还是落回了 prod ——「落回」在结果上与「泳道正常
// 工作」无法区分，只有日志和指标能分辨。

import { Counter } from 'prom-client';

import { resolveInboundDispatch } from './lane-decision';
import { laneInboundPath, type InboundLaneEnvelope } from './lane-envelope';
import { isInboundLaneDispatchEnabled } from './lane-dispatch-flag';
import { getLaneBindingResolver } from '@inner/shared/lane-binding';
import { laneRouter } from '@infrastructure/lane-router';
import { context } from '@middleware/context';
import { register } from '@middleware/metrics';

// 交接的目的地服务名。sidecar 拦的是出向流量，所以这里始终写基础服务名，泳道后缀由它
// 按 x-ctx-lane 解析（LaneRouter.resolveUrl 也不再拼后缀）。
const CHANNEL_SERVER_SERVICE = 'channel-server';

// 交接的超时上限。
//
// **必须严格大于接收端的投影锁等待窗口**（QQ_MESSAGE_PROJECTION_LOCK_TIMEOUT_MS，当前
// 60s）。接收端拿到信封后重走投影，第一件事就是抢同一条 qq_message_id 的锁，在锁上最长
// 排满那个窗口才轮到自己开始干活。投递方的超时小于这个窗口的话，只要接收端在排队就必先
// 超时 —— 而排队不只发生在并发上：那把锁的 release 失败是被吞掉的（common-projector.ts
// 的 finally 只 warn），残留租约有 120s。交接不重试、QQ 那侧早已收下，超时就是这条消息
// 静默消失。这条不等式有测试钉着。
//
// 取 90s：同时盖住 QQ 的 60s 和飞书的 75s（lark-service 那侧同一常量取同一个值，两个
// 渠道的交接失败形状因此一致）。上游没有人在等 —— QQ 平台早已收下，且交接已经移到投影锁
// 之外，挂久一点只占本进程的一个 Promise，不阻塞别人。
//
// 但上限必须有：没有它，sidecar 或对端卡住时这个请求会一直挂着，永远不会有失败信号。
export const LANE_HANDOFF_TIMEOUT_MS = 90_000;

// 交接结果。sidecar 落回 prod 时 HTTP 一样是 200，只能靠对端回报的 handled_by_lane
// 分辨，所以这个维度必须单独记。
const laneHandoffTotal = new Counter({
    name: 'lane_handoff_total',
    help: 'Inbound message handoffs to another lane, by target lane and outcome',
    labelNames: ['channel', 'target_lane', 'outcome'] as const,
    registers: [register],
});

// 送进了目标泳道 / 落回了别处（实际就是 prod）/ 根本没送到。
type HandoffOutcome = 'lane' | 'fallback' | 'error';

export interface InboundDispatchContext {
    // 这条消息是不是交接过来的。交接过的不再判泳道，见 lane-decision.ts。
    handedOff: boolean;
    // 本进程所属 lane（prod channel-server = 'prod'，由 rabbitmq.getLane() 取，
    // 这里要求调用方传 'prod' 或具体 lane，不传 undefined）。
    currentLane: string;
    channel: string;
    botGlobalId: string;
    commonConversationId?: string;
    eventType: string;
    globalMessageId: string;
    traceId: string;
    // 原始平台事件 params，透传进信封供目标 lane channel-server 重走入站。
    params: unknown;
}

/**
 * 这条消息该交给别的 lane 吗？该，就返回备好的信封；不该，返回 null（本地继续走
 * 入站后半段）。**只判定，不投递** —— 投递由调用方在锁外用 handOffToLane 做。
 */
export async function resolveInboundLaneHandoff(
    ctx: InboundDispatchContext,
): Promise<InboundLaneEnvelope | null> {
    const flagEnabled = await isInboundLaneDispatchEnabled();
    const decision = await resolveInboundDispatch({
        flagEnabled,
        handedOff: ctx.handedOff,
        currentLane: ctx.currentLane,
        channel: ctx.channel,
        botGlobalId: ctx.botGlobalId,
        commonConversationId: ctx.commonConversationId,
        resolveLane: (channel, botGlobalId, commonConversationId) =>
            getLaneBindingResolver().resolveLane(channel, botGlobalId, commonConversationId),
    });

    if (decision.action === 'local') {
        return null;
    }

    // bot_name = botGlobalId：bot 维度下全局 bot 标识就是 bot_name，接收端据此
    // 注入 context.botName。
    return {
        channel: ctx.channel,
        event_type: ctx.eventType,
        global_message_id: ctx.globalMessageId,
        trace_id: ctx.traceId,
        lane: decision.lane,
        bot_name: ctx.botGlobalId,
        handed_off: true,
        params: ctx.params,
    };
}

/**
 * 把信封 POST 给目标泳道的 channel-server，等它处理完。
 *
 * 非 2xx、连接失败、超时一律抛（fail-loud，绝不静默当成已送达）。
 * **必须在投影锁之外调用**，理由见文件顶部。
 */
export async function handOffToLane(
    envelope: InboundLaneEnvelope,
    timeoutMs: number = LANE_HANDOFF_TIMEOUT_MS,
): Promise<void> {
    const secret = process.env.INNER_HTTP_SECRET;
    if (!secret) {
        countHandoff(envelope, 'error');
        throw new Error(
            `INNER_HTTP_SECRET is unset; cannot authenticate the lane handoff to ` +
                `${envelope.lane} (gmid=${envelope.global_message_id})`,
        );
    }

    let response: Response;
    try {
        response = await context.run(
            // 以**目标**泳道建立 context：LaneRouter 从 context 读 lane 注入 x-ctx-lane，
            // sidecar 据此选路。qq-gateway 的 inbound-forwarder 用的是自身 lane，语义正好
            // 相反 —— 那是「把消息送进我所在的泳道」，这是「把消息交给别人的泳道」。
            // trace_id / bot_name 也一并放进去，让对端拿到同一条 trace 与 bot 身份。
            context.createContext(envelope.bot_name, envelope.trace_id, envelope.lane),
            () =>
                laneRouter.fetch(CHANNEL_SERVER_SERVICE, laneInboundPath(envelope.channel), {
                    method: 'POST',
                    headers: {
                        Authorization: `Bearer ${secret}`,
                        'Content-Type': 'application/json',
                    },
                    body: JSON.stringify(envelope),
                    // 显式超时上限：没有它，sidecar 或对端卡住时这个请求会一直挂着。
                    signal: AbortSignal.timeout(timeoutMs),
                }),
        );
    } catch (err) {
        countHandoff(envelope, 'error');
        const reason =
            (err as Error).name === 'TimeoutError'
                ? `timed out after ${timeoutMs}ms`
                : `transport error: ${(err as Error).message}`;
        throw new Error(`${describe(envelope)} failed (${reason})`);
    }

    if (!response.ok) {
        countHandoff(envelope, 'error');
        const body = await response.text().catch(() => '');
        throw new Error(
            `${describe(envelope)} failed (HTTP ${response.status}: ${body.slice(0, 200)})`,
        );
    }

    // 对端回报的是**它自己**所在的 lane。与信封 lane 一致 = 泳道收下了；不一致 = 泳道的
    // Service 不在 registry 里，sidecar 把请求打回了 prod。两者 HTTP 上都是 200，结果上
    // 也无法区分，只有这里能留下痕迹。
    const handledByLane = await readHandledLane(response);
    if (handledByLane === envelope.lane) {
        countHandoff(envelope, 'lane');
        console.info(`${describe(envelope)} handled_by_lane=${handledByLane}`);
        return;
    }
    countHandoff(envelope, 'fallback');
    console.warn(
        `${describe(envelope)} fell back: no channel-server in that lane, ` +
            `handled_by_lane=${handledByLane}. The message is processed with the lane's ` +
            `context (downstream lane queues still apply) but by prod code against prod data.`,
    );
}

function describe(envelope: InboundLaneEnvelope): string {
    return (
        `[lane-handoff] channel=${envelope.channel} lane=${envelope.lane} ` +
        `event=${envelope.event_type} gmid=${envelope.global_message_id}`
    );
}

function countHandoff(envelope: InboundLaneEnvelope, outcome: HandoffOutcome): void {
    laneHandoffTotal.inc({
        channel: envelope.channel,
        target_lane: envelope.lane,
        outcome,
    });
}

// 对端在 2xx 里回报自己所在的 lane。读不出来就说 'unknown'：这只影响日志与指标的归类，
// 消息本身已经被处理过了，不该因为回执缺字段而被判成投递失败。
async function readHandledLane(response: Response): Promise<string> {
    const body = (await response.json().catch(() => null)) as { handled_by_lane?: unknown } | null;
    return typeof body?.handled_by_lane === 'string' ? body.handled_by_lane : 'unknown';
}
