import { Hono } from 'hono';

const health = new Hono();

health.get('/api/health', (c) => {
    return c.json({
        status: 'ok',
        service: 'lark-proxy',
        version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
        timestamp: new Date().toISOString(),
    });
});

export default health;
