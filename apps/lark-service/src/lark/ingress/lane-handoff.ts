// 交接的投递侧：这条消息不归本泳道，交给它真正的主人。
//
// lane-inbound.ts 是同一件事的另一头（接住交过来的信封）。信封的形状在 lane-envelope.ts、
// 端点路径在 lane-inbound.ts —— 两侧共用同一份定义，改了字段名或路径不会只改一半。
//
// 判断和投递刻意分开：
//   chooseInboundLane      纯决策，几个分支，不碰任何 I/O
//   handOffToInboundLane   只管打那一次 HTTP，不参与决策
// 混在一起的话，"绑定改了之后消息会不会被二次转投"这种问题就只能连着网络才能验。
//
// ## 这一跳的形状
//
// 打的是本服务自己的服务名 `lark-service`，泳道由 `x-ctx-lane` 交给 sidecar 解析。
// **上下文必须切到目标泳道**：LaneRouter 从 AsyncLocal context 里读 lane 拼那个头，而
// 投递方跑在 prod 上、它自己的上下文说的是 prod —— 照本进程的泳道发就是打给自己。
//（qq-gateway 的 inbound-forwarder 用 selfLane，那是"把我这条泳道的消息交给同泳道的
// 下游"，语义与这里相反。）
//
// 泳道的 Service 不存在时 sidecar 把请求原样打回 prod：**这是设计要的行为**，消息由
// prod 以泳道的上下文处理掉，bot 不会因为泳道没部署而变砖。代价是从投递结果上看不出
// 泳道到底在不在，所以接收端要回报"接住它的是谁"，投递方据此告警。
//
// fail-closed：非 2xx、连接失败、超时一律往上抛。绝不"投不出去就退回本地处理" ——
// 本地处理的是泳道那份改动之外的代码，等于拿线上代码跑了一条本该在泳道验证的消息，
// 而且没有任何信号。也不重试：飞书那侧早就应答过了，重试只会把同一条消息处理两遍。

import { DynamicConfig } from '@inner/shared';
import { context } from '@inner/shared/middleware';
import { Counter } from 'prom-client';

import { register } from '../../server/metrics';

import { LANE_INBOUND_PATH } from './lane-inbound';
import type { InboundLaneEnvelope } from './lane-envelope';

// ---- 判断 ----

export interface InboundLaneChoice {
    /** true = 交给 lane 那条泳道，本进程到此为止。 */
    handOff: boolean;
    lane: string;
}

export interface InboundLaneInput {
    /**
     * 这条事件是交接过来的。**自投循环的唯一阻断点**：泳道的 Service 不存在时
     * sidecar 把交接原样打回 prod，于是 currentLane 又是 'prod'、绑定又指向那条泳道，
     * 不挡就是无限自投。所以这一条先于开关判断 —— 它不是策略，是安全边界。
     */
    handedOff: boolean;
    /** 动态开关。关着的时候连算都不算（也就不打 DB），行为与开关引入之前逐字节一致。 */
    dispatchEnabled: boolean;
    /** 本进程所在泳道。prod 部署是 'prod'。 */
    currentLane: string;
    /** 这条消息按绑定该归哪条泳道。只在真的需要判断时才会被调用。 */
    laneOf: () => Promise<string>;
}

export async function chooseInboundLane(input: InboundLaneInput): Promise<InboundLaneChoice> {
    if (input.handedOff) {
        return { handOff: false, lane: input.currentLane };
    }

    if (!input.dispatchEnabled) {
        return { handOff: false, lane: input.currentLane };
    }

    // 泳道部署手上的消息是 prod 判过一次之后交过来的，信封里的 lane 才是权威。再判
    // 一次的后果很实：绑定在投递之后被改掉时，同一条消息会被二次转投到别的泳道。
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

/** 投递用到的 LaneRouter 表面，就这一个方法。 */
export interface LaneHandoffFetcher {
    fetch(service: string, path: string, init?: RequestInit): Promise<Response>;
}

export interface LaneHandoffDeps {
    /** 打内网 HTTP 的路由器。它按当前上下文的 lane 注入 `x-ctx-lane`。 */
    fetcher: LaneHandoffFetcher;
    /** 内网 Bearer 口令（INNER_HTTP_SECRET），与接收端的鉴权中间件对齐。 */
    innerSecret: string;
    /** 覆盖默认超时上限。只有测试用得上。 */
    timeoutMs?: number;
}

/** 交接打的是本服务自己的服务名，泳道由 sidecar 按 `x-ctx-lane` 解析。 */
const LARK_SERVICE = 'lark-service';

/**
 * 等接收端处理完的上限。
 *
 * **必须严格大于接收端的投影锁等待窗口**（`LARK_MESSAGE_LOCK.waitTimeoutMs`，当前
 * 75s）。接收端拿到信封后重走投影，第一件事就是抢同一条 om_id 的锁，在锁上最长排满
 * 那个窗口才轮到自己开始干活。投递方的超时小于这个窗口的话，只要接收端在排队（同群
 * 多 bot 的正常并发，或前一个持有者留下的残留租约），投递方必先超时 —— 而交接不重
 * 试、飞书早已应答，这条消息就此静默消失。这条不等式有测试钉着。
 *
 * 取 90s：同时盖住飞书的 75s 和 QQ 的 60s（channel-server 那侧同一常量取同一个值，
 * 两个渠道的交接失败形状因此一致）。上游没有人在等 —— 飞书早已应答，且交接已经移到
 * 投影锁之外，挂久一点只占本进程的一个 Promise，不阻塞别人。
 *
 * 但上限必须有：接收端卡住时没有它就是投递方跟着一起卡死，而且连接一直挂着。超时只
 * 代表投递方不再等 —— 接收端那侧可能仍在处理，日志因此要说清楚是"超时"不是"没送到"。
 */
export const LANE_HANDOFF_TIMEOUT_MS = 90_000;

// 与 channel-server 同名同标签，两个渠道的交接可以用同一条查询看。日志能说清单条
// 消息的来龙去脉，但告不了警 —— "绑定指向的泳道压根没部署"要能报出来只能靠这个。
const laneHandoffTotal = new Counter({
    name: 'lane_handoff_total',
    help: 'Inbound message handoffs to another lane, by target lane and outcome',
    labelNames: ['channel', 'target_lane', 'outcome'] as const,
    registers: [register],
});

// 送进了目标泳道 / 落回了别处（实际就是 prod）/ 根本没送到。
type HandoffOutcome = 'lane' | 'fallback' | 'error';

function countOutcome(envelope: InboundLaneEnvelope, outcome: HandoffOutcome): void {
    laneHandoffTotal.inc({
        channel: envelope.channel,
        target_lane: envelope.lane,
        outcome,
    });
}

export async function handOffToInboundLane(
    deps: LaneHandoffDeps,
    envelope: InboundLaneEnvelope,
): Promise<void> {
    const timeoutMs = deps.timeoutMs ?? LANE_HANDOFF_TIMEOUT_MS;
    const label = `lane=${envelope.lane} event=${envelope.event_type} message=${envelope.global_message_id}`;

    // 上下文切到**目标泳道**，LaneRouter 据此拼 x-ctx-lane（见文件头）。trace 也一并
    // 带过去，跨进程之后还能串成一条。
    const ctx = context.createContext(envelope.trace_id, {
        lane: envelope.lane,
        botName: envelope.bot_name,
    });

    let response: Response;
    try {
        response = await context.run(ctx, () =>
            deps.fetcher.fetch(LARK_SERVICE, LANE_INBOUND_PATH, {
                method: 'POST',
                headers: {
                    Authorization: `Bearer ${deps.innerSecret}`,
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify(envelope),
                signal: AbortSignal.timeout(timeoutMs),
            }),
        );
    } catch (error) {
        countOutcome(envelope, 'error');
        const timedOut = (error as Error)?.name === 'TimeoutError';
        throw new Error(
            timedOut
                ? `handing off ${label} timed out after ${timeoutMs}ms; the receiver may still ` +
                  'be working on it, but this process stopped waiting'
                : `handing off ${label} never reached a handler: ${(error as Error).message}`,
        );
    }

    if (!response.ok) {
        countOutcome(envelope, 'error');
        const body = await response.text().catch(() => '');
        throw new Error(
            `handing off ${label} was refused with HTTP ${response.status}: ${body.slice(0, 200)}`,
        );
    }

    reportOutcome(envelope, await handledByLane(response));
}

/** 接收端回报的"接住这次交接的是哪条泳道的进程"。读不出来就说不知道，不猜。 */
async function handledByLane(response: Response): Promise<string> {
    try {
        const body = (await response.json()) as { handled_by_lane?: unknown };
        return typeof body.handled_by_lane === 'string' ? body.handled_by_lane : 'unknown';
    } catch {
        return 'unknown';
    }
}

/**
 * 投递结果的唯一信号。
 *
 * 送达泳道和落回 prod 在投递方眼里都是 200 —— 不在这里分开说，"绑定指向的泳道根本
 * 没部署"就永远查不出来，只会表现为"泳道里的改动怎么没生效"。
 */
function reportOutcome(envelope: InboundLaneEnvelope, handledBy: string): void {
    const label = `event=${envelope.event_type} message=${envelope.global_message_id}`;
    if (handledBy === envelope.lane) {
        countOutcome(envelope, 'lane');
        console.info(`[lark-handoff] lane=${envelope.lane} took it, ${label}`);
        return;
    }
    countOutcome(envelope, 'fallback');
    console.warn(
        `[lark-handoff] handoff for lane=${envelope.lane} was handled by lane=${handledBy} ` +
            `instead: that lane has no lark-service Service, so the message ran on the ` +
            `fallback's code with lane=${envelope.lane} context (${label})`,
    );
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
