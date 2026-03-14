import type { Middleware } from 'koa';
import { AppDataSource } from '../db';
import { AuditLog } from '../entities/audit-log';

/** Map route path to a human-readable action name */
function deriveAction(method: string, path: string): string {
  // Strip /dashboard prefix for matching
  const p = path.replace(/^\/dashboard/, '');

  const patterns: [RegExp, string][] = [
    [/^\/api\/ops\/services\/[^/]+\/pods$/, 'ops.pods.read'],
    [/^\/api\/ops\/services$/, 'ops.services.read'],
    [/^\/api\/ops\/builds\/[^/]+\/latest$/, 'ops.builds.read'],
    [/^\/api\/ops\/db-query$/, 'ops.db-query'],
    [/^\/api\/ops\/lane-bindings$/, method === 'GET' ? 'ops.lane-bindings.read' : method === 'POST' ? 'ops.lane-bindings.create' : 'ops.lane-bindings.delete'],
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

export const auditMiddleware: Middleware = async (ctx, next) => {
  if (!ctx.path.startsWith('/dashboard/api') || SKIP_PATHS.has(ctx.path)) {
    return next();
  }

  const start = Date.now();
  let result: 'success' | 'error' | 'denied' = 'success';
  let errorMessage: string | null = null;

  try {
    await next();
    if (ctx.status === 401 || ctx.status === 403) {
      result = 'denied';
    } else if (ctx.status >= 400) {
      result = 'error';
      const body = ctx.body as Record<string, unknown> | undefined;
      errorMessage = (body?.message as string) || `HTTP ${ctx.status}`;
    }
  } catch (err) {
    result = 'error';
    errorMessage = err instanceof Error ? err.message : String(err);
    throw err;
  } finally {
    const duration = Date.now() - start;
    const caller = ctx.state.caller || 'unknown';
    const action = deriveAction(ctx.method, ctx.path);

    // Build params — omit sensitive fields
    const params: Record<string, unknown> = {};
    if (ctx.query && Object.keys(ctx.query).length) params.query = ctx.query;
    if (ctx.request.body && typeof ctx.request.body === 'object' && Object.keys(ctx.request.body as object).length) {
      const body = { ...(ctx.request.body as Record<string, unknown>) };
      delete body.password;
      delete body.api_key;
      params.body = body;
    }
    if (ctx.params && Object.keys(ctx.params).length) params.params = ctx.params;

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
