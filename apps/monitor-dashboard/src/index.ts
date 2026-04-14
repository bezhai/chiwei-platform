import 'reflect-metadata';
import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { serve } from '@hono/node-server';

import { AppDataSource } from './db';
import { initMongo } from './mongo';
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

const PORT = Number(process.env.DASHBOARD_PORT || 3002);

// Security: JWT secret is required
if (!process.env.DASHBOARD_JWT_SECRET) {
  console.error('FATAL: DASHBOARD_JWT_SECRET is required but not set');
  process.exit(1);
}

const bootstrap = async () => {
  await AppDataSource.initialize();
  await initMongo();

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

  app.route('/dashboard', dashboard);

  serve({ fetch: app.fetch, port: PORT }, () => {
    console.log(`Monitor dashboard server running on ${PORT}`);
  });
};

bootstrap().catch((err) => {
  console.error('Failed to start monitor dashboard:', err);
  process.exit(1);
});
