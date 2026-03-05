import { Hono } from 'hono';
import type { Pool } from 'pg';
import type { LaneResolver } from './lane-resolver';

const PAAS_TOKEN = process.env.PAAS_TOKEN || '';

export function createAdminApp(pool: Pool, laneResolver: LaneResolver): Hono {
    const admin = new Hono().basePath('/api/lark');

    // X-API-Key 认证
    admin.use('*', async (c, next) => {
        if (!PAAS_TOKEN || c.req.header('X-API-Key') !== PAAS_TOKEN) {
            return c.json({ error: 'unauthorized' }, 401);
        }
        await next();
    });

    // 列出所有活跃绑定
    admin.get('/lane-bindings', async (c) => {
        const result = await pool.query(
            'SELECT route_type, route_key, lane_name FROM lane_routing WHERE is_active = true ORDER BY route_type, route_key',
        );
        return c.json({ data: result.rows });
    });

    // 创建/更新绑定 (upsert)
    admin.post('/lane-bindings', async (c) => {
        const { route_type, route_key, lane_name } = await c.req.json<{
            route_type: string;
            route_key: string;
            lane_name: string;
        }>();
        if (!route_type || !route_key || !lane_name) {
            return c.json({ error: 'route_type, route_key, lane_name are required' }, 400);
        }
        await pool.query(
            `INSERT INTO lane_routing (route_type, route_key, lane_name, is_active)
             VALUES ($1, $2, $3, true)
             ON CONFLICT (route_type, route_key) DO UPDATE SET lane_name = $3, is_active = true`,
            [route_type, route_key, lane_name],
        );
        laneResolver.clearCache();
        return c.json({ ok: true, route_type, route_key, lane_name });
    });

    // 删除绑定（软删除）
    admin.delete('/lane-bindings', async (c) => {
        const type = c.req.query('type');
        const key = c.req.query('key');
        if (!type || !key) {
            return c.json({ error: 'type and key query params are required' }, 400);
        }
        await pool.query(
            'UPDATE lane_routing SET is_active = false WHERE route_type = $1 AND route_key = $2',
            [type, key],
        );
        laneResolver.clearCache();
        return c.json({ ok: true });
    });

    return admin;
}
