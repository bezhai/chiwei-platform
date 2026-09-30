import { Hono } from 'hono';
import type { Context } from 'hono';
import type { AppEnv } from '../types';
import { worldClient } from '../paas-client';
import { AppDataSource } from '../db';
import { AuditLog } from '../entities/audit-log';
import { deriveAction } from '../middleware/audit';

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
// 6. 写和删先落审计再转发：先写一条带正文、结果为 pending 的审计，写不进去就 503、不往下
//    转发；转发之后把结果补记到同一条上（补记失败只打日志，先落的那条正文还在）。这一条由
//    路由自己写，共享审计中间件看到 auditWritten 就不再写第二条。列目录和读仍由中间件审计。
// ---------------------------------------------------------------------------

const RECORDS_PATH = '/admin/world/records';
const DOCUMENT_PATH = '/admin/world/records/document';

/** 转发用的出站 client。抽成参数是为了测试能注入替身。 */
export type WorldRecordsClient = {
  get(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>): Promise<unknown>;
  put(path: string, body?: unknown, extraHeaders?: Record<string, string>): Promise<unknown>;
  del(path: string, params?: Record<string, string>, extraHeaders?: Record<string, string>): Promise<unknown>;
};

/** 写和删那一条审计的落库。抽成参数是为了测试能注入替身。 */
export type RecordAuditStore = {
  /** 落一条结果为 pending 的审计，返回它的 id；落不下就抛。 */
  begin(row: { caller: string; action: string; params: Record<string, unknown> }): Promise<number>;
  /** 把结果补记到那一条上。 */
  finish(
    id: number,
    patch: { result: string; error_message: string | null; duration_ms: number; params: Record<string, unknown> },
  ): Promise<void>;
};

export const auditLogStore: RecordAuditStore = {
  async begin(row) {
    const saved = await AppDataSource.getRepository(AuditLog).save({ ...row, result: 'pending' });
    return saved.id;
  },
  async finish(id, patch) {
    await AppDataSource.getRepository(AuditLog).save({ id, ...patch });
  },
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

export function createWorldRecordRoutes(client: WorldRecordsClient, auditStore: RecordAuditStore) {
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

  /**
   * 写和删：先落一条带正文的审计，落不下就拒绝；转发之后补记结果。
   * ``requestParams`` 是这次请求原样的入参（写：请求体；删：查询参数）。
   */
  async function audited(
    c: Context<AppEnv>,
    audit: RecordAudit,
    requestParams: Record<string, unknown>,
    forwardCall: () => Promise<Response>,
  ): Promise<Response> {
    const started = Date.now();
    const caller = c.get('caller') || 'unknown';
    const params = () => ({ ...requestParams, ...audit });
    let id: number;
    try {
      id = await auditStore.begin({ caller, action: deriveAction(c.req.method, c.req.path), params: params() });
    } catch (err) {
      console.error('world records: audit row could not be written; request not forwarded:', err);
      audit.outcome = 'audit_unavailable';
      return c.json({ message: '审计没能落库，这次操作没有转发给 world。' }, 503);
    }
    c.set('auditWritten', true);
    const res = await forwardCall();
    const ok = audit.outcome === 'ok';
    let errorMessage: string | null = null;
    if (!ok) {
      try {
        const body = (await res.clone().json()) as Record<string, unknown>;
        errorMessage = typeof body?.message === 'string' ? body.message : `HTTP ${res.status}`;
      } catch {
        errorMessage = `HTTP ${res.status}`;
      }
    }
    try {
      await auditStore.finish(id, {
        result: ok ? 'success' : 'error',
        error_message: errorMessage,
        duration_ms: Date.now() - started,
        params: params(),
      });
    } catch (err) {
      console.error(`world records: could not record the outcome on audit row ${id}:`, err);
    }
    return res;
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
    return audited(c, audit, { body: fields }, () =>
      forward(
        c,
        audit,
        () => client.put(DOCUMENT_PATH, body, headers(audit, c.get('caller'))),
        (data) => asString(data?.fingerprint),
      ),
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
    return audited(c, audit, { query: params }, () =>
      forward(c, audit, () => client.del(DOCUMENT_PATH, params, headers(audit, c.get('caller')))),
    );
  });

  return app;
}

export default createWorldRecordRoutes(worldClient, auditLogStore);
