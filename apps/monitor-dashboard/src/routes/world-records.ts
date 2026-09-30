import { Hono } from 'hono';
import type { Context } from 'hono';
import type { AppEnv } from '../types';
import { worldClient } from '../paas-client';

// ---------------------------------------------------------------------------
// world 记录的人工读写：转发到 world App 的 /admin/world/records*。
//
// 只给人用：原文灌入初始内容、人工修正、验收时读取。上游要内网凭据（INNER_HTTP_SECRET），
// 开发机拿不到；开发机经 $PAAS_API 打到这里（认 PAAS_TOKEN、落审计），这里带着凭据转过去。
//
// 对外契约：
// 1. 路径、参数、请求体原样交下去。路径规则、字数上限、指纹规矩都只在上游定义。
// 2. 泳道走 header：x-lane / x-ctx-lane 归一成 x-ctx-lane 交给 sidecar 选路。
// 3. 写和删把调用者作为 X-Operator 带下去，上游记日志时写上操作人；审计这一侧另有
//    caller 一列。
// 4. 上游自报的 lane 原样透传。泳道没部署 world 时请求会落回 prod，上游用 409 拒绝并报出
//    自己在哪条泳道；这个 lane 是调用方判断落点的唯一依据。
// 5. 审计里记下请求泳道、执行泳道、哪一份、改之前和改之后的指纹、结果。写的正文在审计的
//    请求体里：人工改过什么，只有这里留着。
// ---------------------------------------------------------------------------

const RECORDS_PATH = '/admin/world/records';
const DOCUMENT_PATH = '/admin/world/records/document';

/** 转发用的出站 client。抽成参数是为了测试能注入替身。 */
export type WorldRecordsClient = {
  get(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>): Promise<unknown>;
  put(path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<unknown>;
  del(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>): Promise<unknown>;
};

type RecordAudit = {
  request_lane: string | null;
  executed_lane: string | null;
  record_path: string | null;
  fingerprint_before: string | null;
  fingerprint_after: string | null;
  outcome: string | null;
};

function laneOf(c: { req: { header: (name: string) => string | undefined } }): string | null {
  return c.req.header('x-ctx-lane') || c.req.header('x-lane') || null;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

export function createWorldRecordRoutes(client: WorldRecordsClient) {
  const app = new Hono<AppEnv>();

  function startAudit(c: Context<AppEnv>, path: unknown, before: unknown): RecordAudit {
    const record: RecordAudit = {
      request_lane: laneOf(c),
      executed_lane: null,
      record_path: asString(path),
      fingerprint_before: asString(before),
      fingerprint_after: null,
      outcome: null,
    };
    c.set('gatewayAudit', record as unknown as Record<string, unknown>);
    return record;
  }

  function headers(audit: RecordAudit, operator?: string): Record<string, string> | undefined {
    const h: Record<string, string> = {};
    if (audit.request_lane) h['x-ctx-lane'] = audit.request_lane;
    if (operator) h['X-Operator'] = operator;
    return Object.keys(h).length ? h : undefined;
  }

  async function forward(
    c: Context<AppEnv>,
    audit: RecordAudit,
    send: () => Promise<unknown>,
    after: (data: Record<string, unknown>) => string | null = () => null,
  ) {
    try {
      const data = (await send()) as Record<string, unknown>;
      audit.executed_lane = asString(data?.lane);
      audit.fingerprint_after = after(data);
      audit.outcome = 'ok';
      return c.json(data);
    } catch (err) {
      const response = (err as { response?: { status?: number; data?: unknown } } | undefined)?.response;
      if (!response || typeof response.status !== 'number') {
        // 上游没回应：不知道落到了哪条泳道、改没改成，就不写 lane。
        audit.outcome = 'upstream_unavailable';
        return c.json({ message: '上游 world 没有回应（超时或连不上），这次操作是否生效未知。' }, 504);
      }
      const data = response.data;
      const detail = data && typeof data === 'object' ? (data as Record<string, unknown>).detail : undefined;
      const d = (detail && typeof detail === 'object' ? detail : {}) as Record<string, unknown>;
      audit.executed_lane = asString(d.lane);
      audit.outcome = 'upstream_error';
      const body: Record<string, unknown> = {
        message: typeof d.message === 'string' ? d.message : `上游返回 HTTP ${response.status}。`,
      };
      if (typeof d.lane === 'string') body.lane = d.lane;
      if (detail !== undefined && typeof detail !== 'object') body.upstream_detail = detail;
      return c.json(body, response.status as never);
    }
  }

  /** GET /api/ops/world/records — 列目录 */
  app.get('/api/ops/world/records', async (c) => {
    const audit = startAudit(c, null, null);
    return forward(c, audit, () => client.get(RECORDS_PATH, {}, headers(audit)));
  });

  /** GET /api/ops/world/records/document?path= — 读一份 */
  app.get('/api/ops/world/records/document', async (c) => {
    const path = c.req.query('path');
    const audit = startAudit(c, path, null);
    const params: Record<string, string> = path ? { path } : {};
    return forward(c, audit, () => client.get(DOCUMENT_PATH, params, headers(audit)));
  });

  /** PUT /api/ops/world/records/document — 写一份（新建不带 fingerprint，改写带它现在的） */
  app.put('/api/ops/world/records/document', async (c) => {
    let body: unknown;
    try {
      body = await c.req.json();
    } catch {
      body = null;
    }
    if (!body || typeof body !== 'object' || Array.isArray(body)) {
      const audit = startAudit(c, null, null);
      audit.outcome = 'invalid_request';
      return c.json({ message: '请求体必须是 JSON 对象' }, 400);
    }
    const fields = body as Record<string, unknown>;
    const audit = startAudit(c, fields.path, fields.fingerprint);
    return forward(
      c,
      audit,
      () => client.put(DOCUMENT_PATH, body, headers(audit, c.get('caller'))),
      (data) => asString(data?.fingerprint),
    );
  });

  /** DELETE /api/ops/world/records/document?path=&fingerprint= — 删一份 */
  app.delete('/api/ops/world/records/document', async (c) => {
    const path = c.req.query('path');
    const fingerprint = c.req.query('fingerprint');
    const audit = startAudit(c, path, fingerprint);
    const params: Record<string, string> = {};
    if (path) params.path = path;
    if (fingerprint) params.fingerprint = fingerprint;
    return forward(c, audit, () => client.del(DOCUMENT_PATH, params, headers(audit, c.get('caller'))));
  });

  return app;
}

export default createWorldRecordRoutes(worldClient);
