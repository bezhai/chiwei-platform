import type { MiddlewareHandler } from 'hono';
import type { AppEnv } from '../types';
import { AppDataSource } from '../db';
import { AuditLog } from '../entities/audit-log';

/** Map route path to a human-readable action name */
export function deriveAction(method: string, path: string): string {
  // Strip /dashboard prefix for matching
  const p = path.replace(/^\/dashboard/, '');

  const patterns: [RegExp, string][] = [
    // gateway-rules：custom method（:explain / :disable / ...）匹配必须排在
    // /{name} 之前，否则带冒号的整段会被 {name} 误吃；snapshot 同理要先于 {name}。
    [/^\/api\/ops\/gateway-rules:explain$/, 'ops.gateway-rules.explain'],
    [/^\/api\/ops\/gateway-rules:rollback$/, 'ops.gateway-rules.rollback'],
    [/^\/api\/ops\/gateway-rules\/[^/]+:disable$/, 'ops.gateway-rules.disable'],
    [/^\/api\/ops\/gateway-rules\/[^/]+:enable$/, 'ops.gateway-rules.enable'],
    [/^\/api\/ops\/gateway-rules\/[^/]+:set-weights$/, 'ops.gateway-rules.set-weights'],
    // snapshots（历史列表）与 snapshot（当前期望配置）都必须排在 /{name} 之前。
    [/^\/api\/ops\/gateway-rules\/snapshots$/, 'ops.gateway-rules.snapshots'],
    [/^\/api\/ops\/gateway-rules\/snapshot$/, 'ops.gateway-rules.snapshot'],
    [/^\/api\/ops\/gateway-rules$/, 'ops.gateway-rules.list'],
    [/^\/api\/ops\/gateway-rules\/[^/]+$/, method === 'PUT' ? 'ops.gateway-rules.update' : method === 'DELETE' ? 'ops.gateway-rules.delete' : 'ops.gateway-rules.get'],
    [/^\/api\/ops\/services\/[^/]+\/pods$/, 'ops.pods.read'],
    [/^\/api\/ops\/services$/, 'ops.services.read'],
    [/^\/api\/ops\/builds\/[^/]+\/latest$/, 'ops.builds.read'],
    [/^\/api\/ops\/db-query$/, 'ops.db-query'],
    [/^\/api\/ops\/lane-bindings$/, method === 'GET' ? 'ops.lane-bindings.read' : method === 'POST' ? 'ops.lane-bindings.create' : 'ops.lane-bindings.delete'],
    // world-documents：动作名里不放文档路径。路径是变长多段的，而
    // audit_logs.action 是 varchar(100)；路径进 params.document_path。
    [/^\/api\/ops\/world-documents$/, 'ops.world-documents.list'],
    [/^\/api\/ops\/world-documents\/document$/, method === 'PUT' ? 'ops.world-documents.write' : method === 'DELETE' ? 'ops.world-documents.delete' : 'ops.world-documents.read'],
    // 这个前缀下走不到路由的请求（多半是把文档路径直接拼进了 URL）会 404，但审计
    // 仍要落一行。兜底动作名会把整段路径拼进去，撑爆 varchar(100) 就整行写不进去。
    [/^\/api\/ops\/world-documents(\/|$)/, 'ops.world-documents.unknown'],
    [/^\/api\/ops\/trigger-diary$/, 'ops.trigger-diary'],
    [/^\/api\/ops\/trigger-weekly-review$/, 'ops.trigger-weekly-review'],
    [/^\/api\/audit-logs$/, 'audit-logs.read'],
    [/^\/api\/activity\//, 'activity.read'],
    [/^\/api\/providers/, method === 'GET' ? 'providers.read' : `providers.${method.toLowerCase()}`],
    [/^\/api\/model-mappings/, method === 'GET' ? 'model-mappings.read' : `model-mappings.${method.toLowerCase()}`],
    [/^\/api\/messages/, 'messages.read'],
    [/^\/api\/service-status/, 'service-status.read'],
    [/^\/api\/migrations/, method === 'POST' ? 'migrations.run' : 'migrations.read'],
    [/^\/api\/mongo/, 'mongo.query'],
  ];

  for (const [re, action] of patterns) {
    if (re.test(p)) return action;
  }
  return `${method.toLowerCase()}.${p.replace(/^\/api\//, '').replace(/\//g, '.')}`;
}

/** Paths that should NOT be audited (high-frequency reads, auth, health) */
const SKIP_PATHS = new Set([
  '/dashboard/api/auth/login',
  '/dashboard/api/config',
  '/dashboard/api/health',
]);

/**
 * 这些前缀下的请求体、查询串和错误消息都不进审计：它们携带世界文档树的正文
 * （整份重写的请求体就是正文，上限 12000 字）。审计要回答的是谁、什么时候、改了
 * 哪一份、从哪个指纹改起，不是改成了什么；全文版本历史是那棵树自己的缺口，不该
 * 靠审计表兜。
 *
 * 排除放在中间件而不是 handler：中间件是在 handler 之前读走 body、在请求结束时
 * 落库的，只在 handler 里覆盖挡不住参数校验提前返回、上游超时和上游抛异常这几条
 * 路径。这些路由要留下的东西由 handler 用 c.set 显式交出来。
 *
 * 前缀按**路由模块自己注册的那个路径**写（和 deriveAction 一样先剥掉 /dashboard
 * 挂载前缀），这样它和路由表是同一个字面量，测试能直接拿注册表逐条对。写成带挂载
 * 前缀的形式，就变成两处能各自漂移的东西，漂了之后正文静默进审计、不报错。
 */
const RAW_INPUT_EXCLUDED_PREFIXES = ['/api/ops/world-documents'];

export function excludesRawInput(path: string): boolean {
  const p = path.replace(/^\/dashboard/, '');
  return RAW_INPUT_EXCLUDED_PREFIXES.some((prefix) => p === prefix || p.startsWith(`${prefix}/`));
}

export const auditMiddleware: MiddlewareHandler<AppEnv> = async (c, next) => {
  if (SKIP_PATHS.has(c.req.path)) {
    return next();
  }

  const start = Date.now();
  const excludeRawInput = excludesRawInput(c.req.path);
  let result: 'success' | 'error' | 'denied' = 'success';
  let errorMessage: string | null = null;

  // Pre-read body for audit logging (Hono caches parsed JSON)
  let requestBody: Record<string, unknown> | null = null;
  if (!excludeRawInput && ['POST', 'PUT', 'PATCH', 'DELETE'].includes(c.req.method)) {
    try {
      requestBody = await c.req.json();
    } catch { /* no body or not JSON */ }
  }

  try {
    await next();
    const status = c.res.status;
    if (status === 401 || status === 403) {
      result = 'denied';
    } else if (status >= 400) {
      result = 'error';
      if (excludeRawInput) {
        // 不读响应体：拒绝原因已经由 handler 放进 params.outcome，而响应里的措辞
        // 是可能回显正文的第三条通道。
        errorMessage = `HTTP ${status}`;
      } else {
        try {
          const resBody = await c.res.clone().json() as Record<string, unknown>;
          errorMessage = (resBody?.message as string) || `HTTP ${status}`;
        } catch {
          errorMessage = `HTTP ${status}`;
        }
      }
    }
  } catch (err) {
    result = 'error';
    // 同理：异常消息也可能带上正文，排除路径下只记异常类型。
    errorMessage = excludeRawInput
      ? (err instanceof Error ? err.name : 'Error')
      : (err instanceof Error ? err.message : String(err));
    throw err;
  } finally {
    const duration = Date.now() - start;
    const caller = c.get('caller') || 'unknown';
    const action = deriveAction(c.req.method, c.req.path);

    // Build params — omit sensitive fields
    const params: Record<string, unknown> = {};

    // Query params
    const queryObj = c.req.queries();
    if (!excludeRawInput && Object.keys(queryObj).length) params.query = queryObj;

    // Request body (pre-read before next())
    if (requestBody && typeof requestBody === 'object' && Object.keys(requestBody).length) {
      const body = { ...requestBody };
      delete body.password;
      delete body.api_key;
      params.body = body;
    }

    // Structured audit payload stashed by a handler (e.g. gateway-rules write ops).
    // Lifted to top-level params keys (rule_name/reason/before/after/snapshot_version)
    // so audit_logs.params is JSONB-queryable by rule_name or snapshot_version.
    // before/after/snapshot_version 的真值取自 paas-engine 写操作响应、不是中转层编的。
    const stashed = c.get('gatewayAudit');
    if (stashed && typeof stashed === 'object') {
      Object.assign(params, stashed);
    }

    // Fire-and-forget audit write
    try {
      const repo = AppDataSource.getRepository(AuditLog);
      await repo.save({
        caller,
        action,
        params: Object.keys(params).length ? params : null,
        result,
        error_message: errorMessage,
        duration_ms: duration,
      });
    } catch (auditErr) {
      console.error('Failed to write audit log:', auditErr);
    }
  }
};
