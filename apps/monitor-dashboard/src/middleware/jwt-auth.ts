import type { Middleware } from 'koa';
import jwt from 'jsonwebtoken';

const PUBLIC_PATHS = new Set([
  '/dashboard/api/auth/login',
  '/dashboard/api/config',
  '/dashboard/api/health',
]);

export const jwtAuth: Middleware = async (ctx, next) => {
  if (!ctx.path.startsWith('/dashboard/api')) {
    return next();
  }
  if (PUBLIC_PATHS.has(ctx.path)) {
    ctx.state.caller = 'public';
    return next();
  }

  // --- API Key auth (Claude Code) ---
  const apiKey = ctx.get('x-api-key');
  const ccToken = process.env.DASHBOARD_CC_TOKEN;
  if (apiKey && ccToken && apiKey === ccToken) {
    ctx.state.caller = 'claude-code';
    return next();
  }

  // --- JWT Bearer auth (Web Admin) ---
  const authHeader = ctx.get('authorization') || ctx.get('Authorization');
  const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : '';

  if (!token) {
    ctx.status = 401;
    ctx.body = { message: 'Unauthorized' };
    return;
  }

  try {
    const secret = process.env.DASHBOARD_JWT_SECRET!;
    const payload = jwt.verify(token, secret);
    ctx.state.user = payload;
    ctx.state.caller = 'web-admin';
  } catch {
    ctx.status = 401;
    ctx.body = { message: 'Unauthorized' };
    return;
  }

  await next();
};
