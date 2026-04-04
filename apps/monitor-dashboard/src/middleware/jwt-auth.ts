import type { MiddlewareHandler } from 'hono';
import jwt from 'jsonwebtoken';
import type { AppEnv } from '../types';

const PUBLIC_PATHS = new Set([
  '/dashboard/api/auth/login',
  '/dashboard/api/config',
  '/dashboard/api/health',
]);

export const jwtAuth: MiddlewareHandler<AppEnv> = async (c, next) => {
  if (PUBLIC_PATHS.has(c.req.path)) {
    c.set('caller', 'public');
    return next();
  }

  // --- API Key auth (Claude Code) ---
  const apiKey = c.req.header('x-api-key');
  const ccToken = process.env.DASHBOARD_CC_TOKEN;
  if (apiKey && ccToken && apiKey === ccToken) {
    c.set('caller', 'claude-code');
    return next();
  }

  // --- JWT Bearer auth (Web Admin) ---
  const authHeader = c.req.header('authorization') || c.req.header('Authorization') || '';
  const token = authHeader.startsWith('Bearer ') ? authHeader.slice(7) : '';

  if (!token) {
    return c.json({ message: 'Unauthorized' }, 401);
  }

  try {
    const secret = process.env.DASHBOARD_JWT_SECRET!;
    const payload = jwt.verify(token, secret);
    c.set('user', payload);
    c.set('caller', 'web-admin');
  } catch {
    return c.json({ message: 'Unauthorized' }, 401);
  }

  await next();
};
