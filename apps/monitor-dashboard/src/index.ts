import 'reflect-metadata';
import Koa from 'koa';
import Router from '@koa/router';
import cors from '@koa/cors';
import bodyParser from 'koa-bodyparser';

import { AppDataSource } from './db';
import { initMongo } from './mongo';
import { jwtAuth } from './middleware/jwt-auth';

import authRoutes from './routes/auth';
import configRoutes from './routes/config';
import tokenStatsRoutes from './routes/token-stats';
import messagesRoutes from './routes/messages';
import providersRoutes from './routes/providers';
import modelMappingsRoutes from './routes/model-mappings';
import mongoRoutes from './routes/mongo';
import migrationsRoutes from './routes/migrations';

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
  app.use(jwtAuth);

  router.use(authRoutes.routes());
  router.use(configRoutes.routes());
  router.use(tokenStatsRoutes.routes());
  router.use(messagesRoutes.routes());
  router.use(providersRoutes.routes());
  router.use(modelMappingsRoutes.routes());
  router.use(mongoRoutes.routes());
  router.use(migrationsRoutes.routes());

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
