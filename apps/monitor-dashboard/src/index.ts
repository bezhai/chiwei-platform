import 'reflect-metadata';
import { serve } from '@hono/node-server';

import { AppDataSource } from './db';
import { initMongo } from './mongo';
import { createDashboardApp } from './app';

const PORT = Number(process.env.DASHBOARD_PORT || 3002);

// Security: JWT secret is required
if (!process.env.DASHBOARD_JWT_SECRET) {
  console.error('FATAL: DASHBOARD_JWT_SECRET is required but not set');
  process.exit(1);
}

const bootstrap = async () => {
  await AppDataSource.initialize();
  await initMongo();

  const app = createDashboardApp();

  serve({ fetch: app.fetch, port: PORT }, () => {
    console.log(`Monitor dashboard server running on ${PORT}`);
  });
};

bootstrap().catch((err) => {
  console.error('Failed to start monitor dashboard:', err);
  process.exit(1);
});
