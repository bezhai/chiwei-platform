// QQ 泳道信封的 HTTP 接收端。
//
// 收的是「另一个进程已经收下这条 QQ 消息、并判定它该由泳道 X 处理」的信封，与
// /api/internal/qq/inbound（qq-gateway 投来的原始 CustomInboundMessage）是两份不同的
// 契约，所以是两条路由而不是一条端点两种报文。
//
// 三条与直觉相反、必须写死的规则：
//
//   信封 lane != 本进程 lane 不是错误
//       sidecar 只在 lite-registry 里查不到目标泳道时才把请求打回 prod。落回 prod 的
//       请求上，信封 lane 必然不等于本进程 lane —— 那是本方案要的行为。所以归属校验
//       只看「这个事件类型是不是本服务认领的」，不看 lane。
//
//   信封的 lane 是权威值
//       x-ctx-lane header 只供 sidecar 选路，途中可能被剥掉或改写；接收端一律按信封的
//       lane 建立 context，下游队列后缀据此决定（见 rules/chat-request.ts 的
//       context.getLane() 优先）。
//
//   缺 handed_off 标记要拒收
//       没有标记的信封会被再判定一次泳道、再交接一次，而落回 prod 的目的地就是 prod
//       自己 —— 无限自投。这是那个循环的唯一阻断点，宁可 400 也不能放行。

import type { Hono } from 'hono';
import { bearerAuthMiddleware } from '@inner/shared/middleware';
import { validateCustomInboundMessage, type CustomInboundMessage } from '@inner/shared/protocols';
import { laneInboundPath, type InboundLaneEnvelope } from '@integrations/lane-envelope';
import { context } from '@middleware/context';

export const QQ_LANE_INBOUND_PATH = laneInboundPath('qq');

// 本服务在泳道信封上认领的范围：QQ 渠道的入站消息事件。飞书的信封归 lark-service，
// 打到这条端点上只可能是投递方发错了地方。
const QQ_CHANNEL = 'qq';
const QQ_INBOUND_EVENT_TYPE = 'qq.message.receive';

/** 报文本身不成立（缺字段、类型不对、没有交接标记）。 */
class InvalidEnvelope extends Error {}

/** 报文成立，但装的不是本服务认领的事件。 */
class UnclaimedEvent extends Error {}

function requireString(raw: Record<string, unknown>, field: string): string {
    const value = raw[field];
    if (typeof value !== 'string' || value.length === 0) {
        throw new InvalidEnvelope(
            `inbound lane envelope field "${field}" must be a non-empty string, ` +
                `got ${JSON.stringify(value)}`,
        );
    }
    return value;
}

// 线格式 → 信封。类型系统管不到 HTTP body，每个字段都得现验。
function readEnvelope(body: unknown): InboundLaneEnvelope {
    if (typeof body !== 'object' || body === null || Array.isArray(body)) {
        throw new InvalidEnvelope(
            `inbound lane envelope must be a JSON object, got ${JSON.stringify(body)}`,
        );
    }
    const raw = body as Record<string, unknown>;
    if (raw.handed_off !== true) {
        throw new InvalidEnvelope(
            'inbound lane envelope carries no handed_off marker; refusing it — an unmarked ' +
                'envelope would be routed through lane resolution again and, when the target ' +
                'lane is absent, handed off to this very process forever',
        );
    }
    return {
        channel: requireString(raw, 'channel'),
        event_type: requireString(raw, 'event_type'),
        global_message_id: requireString(raw, 'global_message_id'),
        trace_id: requireString(raw, 'trace_id'),
        lane: requireString(raw, 'lane'),
        bot_name: requireString(raw, 'bot_name'),
        handed_off: true,
        params: raw.params,
    };
}

function assertClaimed(env: InboundLaneEnvelope): void {
    if (env.channel !== QQ_CHANNEL || env.event_type !== QQ_INBOUND_EVENT_TYPE) {
        throw new UnclaimedEvent(
            `this service does not claim "${env.channel}"/"${env.event_type}" on ` +
                `${QQ_LANE_INBOUND_PATH} (gmid=${env.global_message_id})`,
        );
    }
}

export interface QqLaneInboundDeps {
    /** 走入站处理链。context 由本模块按信封建立好之后再调。 */
    handle: (message: CustomInboundMessage) => Promise<void>;
    /** 本进程实际所在的 lane（prod 部署为 'prod'）。回给投递方判断有没有落回 prod。 */
    processLane: () => string;
}

export function registerQqLaneInbound(app: Hono, deps: QqLaneInboundDeps): void {
    app.post(QQ_LANE_INBOUND_PATH, bearerAuthMiddleware, async (c) => {
        let env: InboundLaneEnvelope;
        let message: CustomInboundMessage;
        try {
            env = readEnvelope(await c.req.json());
            assertClaimed(env);
            message = validateCustomInboundMessage(env.params);
        } catch (err) {
            const status = err instanceof UnclaimedEvent ? 422 : 400;
            console.warn(`[qq lane-inbound] rejected with ${status}: ${(err as Error).message}`);
            return c.json({ success: false, message: (err as Error).message }, status);
        }

        const handledByLane = deps.processLane();
        try {
            await context.run(context.createContext(env.bot_name, env.trace_id, env.lane), () =>
                deps.handle(message),
            );
        } catch (err) {
            // 处理失败必须是非 2xx：投递方不重试，状态码是它唯一能知道「这条消息没人
            // 处理」的途径。吞掉异常返回 200 等于静默丢消息。
            console.error(
                `[qq lane-inbound] handling failed: lane=${env.lane} ` +
                    `handled_by_lane=${handledByLane} gmid=${env.global_message_id} ` +
                    `detail=${(err as Error).message}`,
            );
            return c.json({ success: false, message: (err as Error).message }, 500);
        }
        return c.json({ success: true, handled_by_lane: handledByLane });
    });

    // 排查交接 404 时唯一能确认「接收端挂上了」的凭据。措辞与 lark-service 的
    // lark/ingress/lane-inbound.ts 对齐，`lane-inbound] registered at` 一条查询同时覆盖
    // 两个渠道。
    console.info(
        `[qq lane-inbound] registered at ${QQ_LANE_INBOUND_PATH} on lane=${deps.processLane()}`,
    );
}
