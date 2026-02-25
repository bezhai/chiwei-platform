import Koa from 'koa';
import Router from '@koa/router';
import { Pool } from 'pg';
import { LaneResolver } from './lane-resolver';
import { EventForwarder } from './forwarder';
import { BotManager } from './bot-manager';
import healthRouter from './health';

const PORT = parseInt(process.env.LARK_PROXY_PORT || '3003', 10);

async function main(): Promise<void> {
    const pool = new Pool({
        host: process.env.POSTGRES_HOST,
        port: parseInt(process.env.POSTGRES_PORT || '5432', 10),
        user: process.env.POSTGRES_USER,
        password: process.env.POSTGRES_PASSWORD,
        database: process.env.POSTGRES_DB,
    });

    // 验证 DB 连接
    await pool.query('SELECT 1');
    console.info('PostgreSQL connected');

    const laneResolver = new LaneResolver(pool);
    const forwarder = new EventForwarder(laneResolver);
    const botManager = new BotManager(pool, forwarder);

    const app = new Koa();
    const router = new Router();

    // 注册 health check
    app.use(healthRouter.routes());

    // 注册所有 bot webhook 路由
    await botManager.registerBots(router);

    app.use(router.routes());
    app.use(router.allowedMethods());

    app.listen(PORT, () => {
        console.info(`lark-proxy listening on port ${PORT}`);
    });
}

main().catch((err) => {
    console.error('lark-proxy failed to start:', err);
    process.exit(1);
});
