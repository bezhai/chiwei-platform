import type { MiddlewareHandler } from 'hono';
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

export const auditMiddleware: MiddlewareHandler = async (c, next) => {
  if (SKIP_PATHS.has(c.req.path)) {
    return next();
  }

  const start = Date.now();
  let result: 'success' | 'error' | 'denied' = 'success';
  let errorMessage: string | null = null;

  try {
    await next();
    const status = c.res.status;
    if (status === 401 || status === 403) {
      result = 'denied';
    } else if (status >= 400) {
      result = 'error';
      try {
        const resBody = await c.res.clone().json() as Record<string, unknown>;
        errorMessage = (resBody?.message as string) || `HTTP ${status}`;
      } catch {
        errorMessage = `HTTP ${status}`;
      }
    }
  } catch (err) {
    result = 'error';
    errorMessage = err instanceof Error ? err.message : String(err);
    throw err;
  } finally {
    const duration = Date.now() - start;
    const caller = c.get('caller') || 'unknown';
    const action = deriveAction(c.req.method, c.req.path);

    // Build params — omit sensitive fields
    const params: Record<string, unknown> = {};

    // Query params
    const url = new URL(c.req.url);
    const queryObj: Record<string, string> = {};
    url.searchParams.forEach((v, k) => { queryObj[k] = v; });
    if (Object.keys(queryObj).length) params.query = queryObj;

    // Request body (only for methods that typically have a body)
    if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(c.req.method)) {
      try {
        const reqBody = await c.req.json() as Record<string, unknown>;
        if (reqBody && typeof reqBody === 'object' && Object.keys(reqBody).length) {
          const body = { ...reqBody };
          delete body.password;
          delete body.api_key;
          params.body = body;
        }
      } catch {
        // No JSON body or already consumed — skip
      }
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
