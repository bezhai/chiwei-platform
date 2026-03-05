import { Hono } from 'hono';
import { Pool } from 'pg';
import { LaneResolver } from './lane-resolver';
import { EventForwarder } from './forwarder';
import { BotManager } from './bot-manager';
import healthApp from './health';
import { laneRouter } from './lane-router-instance';
import { metricsMiddleware, metricsApp } from './metrics';
import { createAdminApp } from './admin';

const PORT = parseInt(process.env.LARK_PROXY_PORT || '3003', 10);

const pool = new Pool({
    host: process.env.POSTGRES_HOST,
    port: parseInt(process.env.POSTGRES_PORT || '5432', 10),
    user: process.env.POSTGRES_USER,
    password: process.env.POSTGRES_PASSWORD,
    database: process.env.POSTGRES_DB,
});

await pool.query('SELECT 1');
console.info('PostgreSQL connected');

const laneResolver = new LaneResolver(pool);
const forwarder = new EventForwarder(laneResolver, laneRouter);
const botManager = new BotManager(pool, forwarder);

const app = new Hono();

// Prometheus metrics
app.use('*', metricsMiddleware);
app.route('', metricsApp);

// Health check
app.route('', healthApp);

// 管理 API（泳道绑定）
const adminApp = createAdminApp(pool, laneResolver);
app.route('', adminApp);

// Bot webhook 路由
await botManager.registerBots(app);

console.info(`lark-proxy listening on port ${PORT}`);

export default { port: PORT, fetch: app.fetch };
