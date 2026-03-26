import 'reflect-metadata';
import Koa from 'koa';
import Router from '@koa/router';
import cors from '@koa/cors';
import bodyParser from 'koa-bodyparser';

import { AppDataSource } from './db';
import { initMongo } from './mongo';
import { jwtAuth } from './middleware/jwt-auth';
import { auditMiddleware } from './middleware/audit';

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

const PORT = Number(process.env.DASHBOARD_PORT || 3002);

// Security: JWT secret is required
if (!process.env.DASHBOARD_JWT_SECRET) {
  console.error('FATAL: DASHBOARD_JWT_SECRET is required but not set');
  process.exit(1);
}

const bootstrap = async () => {
  await AppDataSource.initialize();
  await initMongo();

  const app = new Koa();
  const router = new Router({ prefix: '/dashboard' });

  if (process.env.NODE_ENV !== 'production') {
    app.use(cors());
  }

  app.use(bodyParser({ jsonLimit: '2mb' }));

  // Global error handler — return JSON instead of crashing
  app.use(async (ctx, next) => {
    try {
      await next();
    } catch (err: unknown) {
      const axiosResp = (err as { response?: { status?: number; data?: unknown } })?.response;
      const status = axiosResp?.status
        || (err as { status?: number })?.status
        || 500;
      // Prefer upstream error body (e.g. PaaS Engine's {"error":"..."})
      const upstream = axiosResp?.data;
      const message = (upstream && typeof upstream === 'object' && 'error' in upstream)
        ? (upstream as Record<string, unknown>).error
        : err instanceof Error ? err.message : String(err);
      ctx.status = status;
      ctx.body = { message, status };
    }
  });

  app.use(jwtAuth);
  app.use(auditMiddleware);

  router.use(authRoutes.routes());
  router.use(configRoutes.routes());
  router.use(messagesRoutes.routes());
  router.use(providersRoutes.routes());
  router.use(modelMappingsRoutes.routes());
  router.use(mongoRoutes.routes());
  router.use(migrationsRoutes.routes());
  router.use(serviceStatusRoutes.routes());
  router.use(operationsRoutes.routes());
  router.use(auditLogsRoutes.routes());
  router.use(activityRoutes.routes());

  app.use(router.routes());
  app.use(router.allowedMethods());

  app.listen(PORT, () => {
    console.log(`Monitor dashboard server running on ${PORT}`);
  });
};

bootstrap().catch((err) => {
  console.error('Failed to start monitor dashboard:', err);
  process.exit(1);
});
