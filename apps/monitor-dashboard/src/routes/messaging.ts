import { Hono } from 'hono';
import type { Context } from 'hono';
import type { AppEnv } from '../types';
import { agentClient } from '../paas-client';

// ---------------------------------------------------------------------------
// 通信机制的人工入口：转发到 agent-service 的 /admin/messaging/*。
//
// 那四条要内网凭据（INNER_HTTP_SECRET），开发机拿不到；开发机经 $PAAS_API 打到这里
// （认 PAAS_TOKEN、落审计），这里带着凭据转过去。
//
// 对外契约：
// 1. 请求体和查询参数原样交下去。参与者名字、时刻格式、等待时长这些规则只在上游定义，
//    这里再写一遍就是两处会各自漂移的东西。
// 2. 泳道走 header：x-lane / x-ctx-lane 归一成 x-ctx-lane 交给 sidecar 选路。
// 3. 上游自报的 lane 原样透传。泳道没部署 agent-service 时请求会落回 prod，上游会用
//    409 拒绝并报出自己在哪条泳道；这个 lane 是调用方判断落点的唯一依据。上游拒绝时
//    带回的 message_id 同样原样透传（沿用原 id 重试靠它）。
// 4. 提问要等对方回答：出站超时按提问方给的等待时长放宽，不被默认的 15 秒截断。
// ---------------------------------------------------------------------------

const SEND_PATH = '/admin/messaging/send';
const ASK_PATH = '/admin/messaging/ask';
const SEND_AT_PATH = '/admin/messaging/send-at';
const RECORD_PATH = '/admin/messaging/record';
const DEAD_LETTERS_PATH = '/admin/messaging/dead-letters';
const REPLAY_PATH = '/admin/messaging/dead-letters/replay';

/** 上游 /admin/messaging/ask 不给 timeout_seconds 时的默认等待时长（秒）。 */
const ASK_DEFAULT_SECONDS = 60;
/** 在提问方的等待时长之外再留给这一跳的余量（毫秒）。 */
const ASK_MARGIN_MS = 15_000;

/** 转发用的出站 client。抽成参数是为了测试能注入替身。 */
export type AgentMessagingClient = {
  get(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>): Promise<unknown>;
  post(
    path: string,
    body?: unknown,
    extraHeaders?: Record<string, string>,
    options?: { timeoutMs?: number },
  ): Promise<unknown>;
};

type MessagingAudit = {
  request_lane: string | null;
  executed_lane: string | null;
  message_id: string | null;
  outcome: string | null;
};

function laneOf(c: { req: { header: (name: string) => string | undefined } }): string | null {
  return c.req.header('x-ctx-lane') || c.req.header('x-lane') || null;
}

function laneHeaders(lane: string | null): Record<string, string> | undefined {
  return lane ? { 'x-ctx-lane': lane } : undefined;
}

export function createMessagingRoutes(client: AgentMessagingClient) {
  const app = new Hono<AppEnv>();

  function startAudit(c: Context<AppEnv>): MessagingAudit {
    const record: MessagingAudit = {
      request_lane: laneOf(c),
      executed_lane: null,
      message_id: null,
      outcome: null,
    };
    c.set('gatewayAudit', record as unknown as Record<string, unknown>);
    return record;
  }

  async function forward(c: Context<AppEnv>, audit: MessagingAudit, send: () => Promise<unknown>) {
    try {
      const data = (await send()) as Record<string, unknown>;
      audit.executed_lane = typeof data?.lane === 'string' ? data.lane : null;
      const id = data?.message_id ?? data?.question_id;
      audit.message_id = typeof id === 'string' ? id : null;
      audit.outcome = 'ok';
      return c.json(data);
    } catch (err) {
      const response = (err as { response?: { status?: number; data?: unknown } } | undefined)?.response;
      if (!response || typeof response.status !== 'number') {
        // 上游没回应：不知道落到了哪条泳道、发没发出去，就不写 lane。
        audit.outcome = 'upstream_unavailable';
        return c.json({ message: '上游 agent-service 没有回应（超时或连不上），这次操作是否生效未知。' }, 504);
      }
      const data = response.data;
      const detail = data && typeof data === 'object' ? (data as Record<string, unknown>).detail : undefined;
      const d = (detail && typeof detail === 'object' ? detail : {}) as Record<string, unknown>;
      audit.executed_lane = typeof d.lane === 'string' ? d.lane : null;
      audit.outcome = 'upstream_error';
      const body: Record<string, unknown> = {
        message: typeof d.message === 'string' ? d.message : `上游返回 HTTP ${response.status}。`,
      };
      if (typeof d.lane === 'string') body.lane = d.lane;
      // 发送失败时上游带回这条消息的 id：消息可能已经到了对方收件箱，调用方要沿用这个 id
      // 重试，接收方才能按 id 去重。丢掉它，重试就会变成一条新消息。
      if (typeof d.message_id === 'string') {
        body.message_id = d.message_id;
        audit.message_id = d.message_id;
      }
      if (detail !== undefined && typeof detail !== 'object') body.upstream_detail = detail;
      return c.json(body, response.status as never);
    }
  }

  async function jsonObject(c: Context<AppEnv>): Promise<Record<string, unknown> | null> {
    try {
      const parsed = await c.req.json();
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
      return parsed as Record<string, unknown>;
    } catch {
      return null;
    }
  }

  function postRoute(
    route: string,
    upstream: string,
    extra: { timeoutFor?: (body: Record<string, unknown>) => number; withOperator?: boolean } = {},
  ) {
    app.post(route, async (c) => {
      const audit = startAudit(c);
      const body = await jsonObject(c);
      if (!body) {
        audit.outcome = 'invalid_request';
        return c.json({ message: '请求体必须是 JSON 对象' }, 400);
      }
      const options = extra.timeoutFor ? { timeoutMs: extra.timeoutFor(body) } : undefined;
      let headers = laneHeaders(audit.request_lane);
      const caller = c.get('caller');
      if (extra.withOperator && caller) headers = { ...(headers || {}), 'X-Operator': caller };
      return forward(c, audit, () => client.post(upstream, body, headers, options));
    });
  }

  function getRoute(route: string, upstream: string, keys: string[]) {
    app.get(route, async (c) => {
      const audit = startAudit(c);
      const params: Record<string, string> = {};
      for (const key of keys) {
        const value = c.req.query(key);
        if (value) params[key] = value;
      }
      return forward(c, audit, () => client.get(upstream, params, laneHeaders(audit.request_lane)));
    });
  }

  /** POST /api/ops/messaging/send — 以任意身份发给某个参与者 */
  postRoute('/api/ops/messaging/send', SEND_PATH);

  /** POST /api/ops/messaging/ask — 以任意身份提问，等回答 */
  postRoute('/api/ops/messaging/ask', ASK_PATH, {
    timeoutFor: (body) => {
      const seconds = typeof body.timeout_seconds === 'number' ? body.timeout_seconds : ASK_DEFAULT_SECONDS;
      return seconds * 1000 + ASK_MARGIN_MS;
    },
  });

  /** POST /api/ops/messaging/send-at — 以任意身份定一条指定时刻送达的消息 */
  postRoute('/api/ops/messaging/send-at', SEND_AT_PATH);

  /** GET /api/ops/messaging/record — 查通信记录 */
  getRoute('/api/ops/messaging/record', RECORD_PATH, ['message_id', 'participant', 'limit']);

  /** GET /api/ops/messaging/dead-letters — 看本泳道的死信（上游看完原样放回） */
  getRoute('/api/ops/messaging/dead-letters', DEAD_LETTERS_PATH, ['limit']);

  /** POST /api/ops/messaging/dead-letters/replay — 把本泳道的死信发回原队列；调用者作为 X-Operator 记进上游审计 */
  postRoute('/api/ops/messaging/dead-letters/replay', REPLAY_PATH, { withOperator: true });

  return app;
}

export default createMessagingRoutes(agentClient);
