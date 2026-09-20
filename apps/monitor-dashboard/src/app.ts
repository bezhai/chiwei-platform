import 'reflect-metadata';
import { Hono } from 'hono';
import { cors } from 'hono/cors';

import { jwtAuth } from './middleware/jwt-auth';
import { auditMiddleware } from './middleware/audit';
import { createContextPropagationMiddleware } from '@inner/shared/middleware';

import authRoutes from './routes/auth';
import configRoutes from './routes/config';
import messagesRoutes from './routes/messages';
import providersRoutes from './routes/providers';
import modelMappingsRoutes from './routes/model-mappings';
import mongoRoutes from './routes/mongo';
import migrationsRoutes from './routes/migrations';
import serviceStatusRoutes from './routes/service-status';
import operationsRoutes from './routes/operations';
import auditLogsRoutes from './routes/audit-logs';
import activityRoutes from './routes/activity';
import dynamicConfigRoutes from './routes/dynamic-config';
import skillsRoutes from './routes/skills';
import worldDocumentsRoutes from './routes/world-documents';

/**
 * 组装整个 dashboard：中间件顺序、挂载前缀、路由注册。
 *
 * 单独成一个函数是为了测试能跑在**生产的这一份组装**上，而不是各自手搭一个 Hono
 * app 把中间件挂上去——自搭的话中间件顺序一改就不是同一条路了，而审计脱敏这类性质
 * 恰恰依赖顺序（jwtAuth 在 auditMiddleware 之前，所以鉴权拒绝时审计根本不跑）。
 *
 * 这里不碰连接：DB 与 Mongo 的初始化留在 index.ts 的启动流程里。
 */
export function createDashboardApp(): Hono {
  const app = new Hono();

  if (process.env.NODE_ENV !== 'production') {
    app.use(cors());
  }

  // Global error handler — return JSON instead of crashing
  app.onError((err, c) => {
    const axiosResp = (err as any)?.response;
    const status = axiosResp?.status || (err as any)?.status || 500;
    const raw = axiosResp?.data;
    const upstream = (raw && typeof raw === 'object' && 'data' in raw) ? raw.data : raw;
    const message = (upstream && typeof upstream === 'object' && 'error' in upstream)
      ? upstream.error
      : err.message;
    return c.json({ message, status }, status);
  });

  // Context propagation (x-ctx-* headers) — must be before route handlers
  app.use('*', createContextPropagationMiddleware());

  // Auth & audit middleware on API routes
  app.use('/dashboard/api/*', jwtAuth);
  app.use('/dashboard/api/*', auditMiddleware);

  // Mount route sub-apps under /dashboard
  const dashboard = new Hono();
  dashboard.route('/', authRoutes);
  dashboard.route('/', configRoutes);
  dashboard.route('/', messagesRoutes);
  dashboard.route('/', providersRoutes);
  dashboard.route('/', modelMappingsRoutes);
  dashboard.route('/', mongoRoutes);
  dashboard.route('/', migrationsRoutes);
  dashboard.route('/', serviceStatusRoutes);
  dashboard.route('/', operationsRoutes);
  dashboard.route('/', auditLogsRoutes);
  dashboard.route('/', activityRoutes);
  dashboard.route('/', dynamicConfigRoutes);
  dashboard.route('/', skillsRoutes);
  dashboard.route('/', worldDocumentsRoutes);

  app.route('/dashboard', dashboard);

  return app;
}
