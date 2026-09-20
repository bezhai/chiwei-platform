import { Hono } from 'hono';
import type { Context } from 'hono';
import type { AppEnv } from '../types';
import { agentClient } from '../paas-client';

// ---------------------------------------------------------------------------
// 世界文档树的带审计入口：转发到 agent-service 的 /admin/world-documents/*。
//
// 文档树的卷只挂在 agent-service 上，写入安全靠的是那个进程内的两把锁 + 指纹 CAS，
// 所以 dashboard 只能转发 HTTP，不能自己挂卷读写。
//
// 三条对外契约：
// 1. 上游响应里的 lane 原样透传。它是上游进程按自己的部署环境自报的，说的是"这次
//    调用实际改的是哪棵树"——泳道不在注册表里时请求会静默落回 prod 并返回 200，
//    这个字段是调用方发现落错地方的唯一途径。不吞、不改、不拿请求里的泳道替换。
// 2. 泳道走 header：把 x-lane / x-ctx-lane 归一成 x-ctx-lane 交给 sidecar 选路。
//    不写成 ?lane=，那是控制面查询参数的写法，用在这里请求会永远落在 prod。
// 3. 上游 409 的 message 是写给模型看的中文，里面提的是模型手上的工具名。HTTP 调用
//    方没有那只手，所以对外措辞按 outcome 自己组织，上游原句放在 upstream_message。
// ---------------------------------------------------------------------------

const LISTING_PATH = '/admin/world-documents/listing';
const DOCUMENT_PATH = '/admin/world-documents/document';

/** 转发用的出站 client。抽成参数是为了测试能注入替身，不必靠进程级的模块 mock。 */
export type AgentDocumentsClient = {
  get(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>): Promise<unknown>;
  put(path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<unknown>;
  del(
    path: string,
    params?: Record<string, string>,
    extraHeaders?: Record<string, string>,
    body?: unknown,
  ): Promise<unknown>;
};

/** 落进审计的结构化字段。正文本身不在其中——只留长度。 */
type DocumentAudit = {
  document_path: string | null;
  request_lane: string | null;
  executed_lane: string | null;
  fingerprint: string | null;
  content_length: number | null;
  outcome: string | null;
};

/** 按 outcome 组织的对外措辞。上游那几句是写给模型看的，不能直接转给人。 */
const OUTCOME_MESSAGES: Record<string, string> = {
  no_fingerprint: '这份文档已经存在，本次操作必须带上读到它时的指纹。',
  stale_fingerprint: '指纹已过期：读到它之后这份文档被改过，请重新读取再操作。',
  // 上游的 gone 是写和删共用的一个值：写那边是"你读到它之后它被删掉了"，删那边是
  // "你要删的那一份已经不在了"。对调用方是同一件事——那一份整个没了，不是某个版本。
  gone: '这份文档整个不在了：你读到它之后它被删掉了。请重新读一遍再决定下一步；确实要重新建一份的话，不带指纹再写一次。',
};

type UpstreamFailure = {
  status: number;
  lane: string | null;
  path: string | null;
  outcome: string | null;
  upstreamMessage: string | null;
};

/** 把 axios 抛出来的东西读成上游拒绝；上游根本没回应时返回 null。 */
function readUpstreamFailure(err: unknown): UpstreamFailure | null {
  const response = (err as { response?: { status?: number; data?: unknown } } | undefined)?.response;
  if (!response || typeof response.status !== 'number') return null;

  const data = response.data;
  const detail = (data && typeof data === 'object' ? (data as Record<string, unknown>).detail : undefined);
  const d = (detail && typeof detail === 'object' ? detail : {}) as Record<string, unknown>;

  return {
    status: response.status,
    lane: typeof d.lane === 'string' ? d.lane : null,
    path: typeof d.path === 'string' ? d.path : null,
    outcome: typeof d.outcome === 'string' ? d.outcome : null,
    upstreamMessage: typeof d.message === 'string' ? d.message : null,
  };
}

function messageForFailure(f: UpstreamFailure): string {
  if (f.outcome && OUTCOME_MESSAGES[f.outcome]) return OUTCOME_MESSAGES[f.outcome];
  if (f.outcome) return `上游拒绝了这次操作（outcome=${f.outcome}）。`;
  if (f.status === 400) return '上游拒绝了请求参数。';
  if (f.status === 404) return '上游没有找到这份文档。';
  if (f.status === 401 || f.status === 403) return '上游拒绝了这次调用的凭据。';
  return `上游返回 HTTP ${f.status}。`;
}

/** 请求里的泳道归一成 sidecar 认的那一个 header */
function laneOf(c: { req: { header: (name: string) => string | undefined } }): string | null {
  return c.req.header('x-ctx-lane') || c.req.header('x-lane') || null;
}

function laneHeaders(lane: string | null): Record<string, string> | undefined {
  return lane ? { 'x-ctx-lane': lane } : undefined;
}

export function createWorldDocumentsRoutes(client: AgentDocumentsClient) {
  const app = new Hono<AppEnv>();

  /**
   * 建审计记录并立刻挂到 context 上。返回的是同一个对象，后面直接改字段——
   * 审计中间件是在请求结束时才读它的，所以提前挂、边走边补，哪条分支提前返回或
   * 抛出去都还留得下记录。
   */
  function startAudit(c: Context<AppEnv>, seed: Partial<DocumentAudit>): DocumentAudit {
    const record: DocumentAudit = {
      document_path: null,
      request_lane: null,
      executed_lane: null,
      fingerprint: null,
      content_length: null,
      outcome: null,
      ...seed,
    };
    c.set('gatewayAudit', record as unknown as Record<string, unknown>);
    return record;
  }

  /** 统一处理转发结果：成功原样回，失败按 outcome 翻译。 */
  async function forward(c: Context<AppEnv>, audit: DocumentAudit, send: () => Promise<unknown>) {
    try {
      const data = (await send()) as Record<string, unknown>;
      audit.executed_lane = typeof data?.lane === 'string' ? data.lane : null;
      audit.outcome = typeof data?.outcome === 'string' ? data.outcome : 'ok';
      return c.json(data);
    } catch (err) {
      const failure = readUpstreamFailure(err);
      if (!failure) {
        // 上游没回应（超时 / 连不上）。这时候不知道落到了哪棵树，就不写 lane——
        // 拿请求里的泳道充数等于伪造了这次调用唯一的落点证据。
        audit.outcome = 'upstream_unavailable';
        return c.json({ message: '上游 agent-service 没有回应（超时或连不上），这次操作是否生效未知。' }, 504);
      }
      audit.executed_lane = failure.lane;
      audit.outcome = failure.outcome ?? 'upstream_error';

      const body: Record<string, unknown> = { message: messageForFailure(failure) };
      if (failure.lane !== null) body.lane = failure.lane;
      if (failure.path !== null) body.path = failure.path;
      if (failure.outcome !== null) body.outcome = failure.outcome;
      if (failure.upstreamMessage !== null) body.upstream_message = failure.upstreamMessage;
      return c.json(body, failure.status as never);
    }
  }

  /** GET /api/ops/world-documents — 列目录 */
  app.get('/api/ops/world-documents', async (c) => {
    const lane = laneOf(c);
    const under = c.req.query('under');
    const audit = startAudit(c, { document_path: under ?? null, request_lane: lane });

    const params: Record<string, string> = {};
    if (under) params.under = under;
    return forward(c, audit, () => client.get(LISTING_PATH, params, laneHeaders(lane)));
  });

  /** GET /api/ops/world-documents/document — 读一份 */
  app.get('/api/ops/world-documents/document', async (c) => {
    const lane = laneOf(c);
    const path = c.req.query('path');
    const audit = startAudit(c, { document_path: path ?? null, request_lane: lane });

    if (!path) {
      audit.outcome = 'invalid_request';
      return c.json({ message: 'path 查询参数必填' }, 400);
    }
    return forward(c, audit, () => client.get(DOCUMENT_PATH, { path }, laneHeaders(lane)));
  });

  /** PUT /api/ops/world-documents/document — 整份重写 */
  app.put('/api/ops/world-documents/document', async (c) => {
    const lane = laneOf(c);
    const audit = startAudit(c, { request_lane: lane });

    let body: Record<string, unknown>;
    try {
      const parsed = await c.req.json();
      if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('not an object');
      body = parsed as Record<string, unknown>;
    } catch {
      audit.outcome = 'invalid_request';
      return c.json({ message: '请求体必须是 JSON 对象' }, 400);
    }

    const path = body.path;
    const content = body.content;
    const fingerprint = body.fingerprint;

    audit.document_path = typeof path === 'string' ? path : null;
    audit.fingerprint = typeof fingerprint === 'string' ? fingerprint : null;
    audit.content_length = typeof content === 'string' ? content.length : null;

    if (typeof path !== 'string' || path === '') {
      audit.outcome = 'invalid_request';
      return c.json({ message: 'path 必填，且必须是非空字符串' }, 400);
    }
    if (typeof content !== 'string') {
      audit.outcome = 'invalid_request';
      return c.json({ message: 'content 必填，且必须是字符串' }, 400);
    }
    if (fingerprint !== undefined && typeof fingerprint !== 'string') {
      audit.outcome = 'invalid_request';
      return c.json({ message: 'fingerprint 必须是字符串' }, 400);
    }

    // 正文长度上限由上游判定，不在这里抄一份阈值——抄了就是两处定义。
    const payload: Record<string, unknown> = { path, content };
    if (typeof fingerprint === 'string') payload.fingerprint = fingerprint;

    return forward(c, audit, () => client.put(DOCUMENT_PATH, payload, laneHeaders(lane)));
  });

  /** DELETE /api/ops/world-documents/document — 删一份 */
  app.delete('/api/ops/world-documents/document', async (c) => {
    const lane = laneOf(c);
    const path = c.req.query('path');
    const fingerprint = c.req.query('fingerprint');
    const audit = startAudit(c, {
      document_path: path ?? null,
      fingerprint: fingerprint ?? null,
      request_lane: lane,
    });

    if (!path) {
      audit.outcome = 'invalid_request';
      return c.json({ message: 'path 查询参数必填' }, 400);
    }
    if (!fingerprint) {
      audit.outcome = 'invalid_request';
      return c.json({ message: 'fingerprint 查询参数必填：删除必须带上读到这份文档时的指纹' }, 400);
    }

    return forward(c, audit, () => client.del(DOCUMENT_PATH, { path, fingerprint }, laneHeaders(lane)));
  });

  return app;
}

export default createWorldDocumentsRoutes(agentClient);
