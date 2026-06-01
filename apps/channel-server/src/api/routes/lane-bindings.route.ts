import { Hono, type Context, type Next } from 'hono';
import AppDataSource from '@ormconfig';
import { getLaneRouter } from '@integrations/lane-router-runtime';

const app = new Hono();

function authorized(apiKey: string | undefined): boolean {
    const token = process.env.PAAS_TOKEN;
    return Boolean(token && apiKey === token);
}

async function requireApiKey(c: Context, next: Next) {
    if (!authorized(c.req.header('X-API-Key'))) {
        return c.json({ error: 'unauthorized' }, 401);
    }
    await next();
}

app.use('/api/lane-bindings', requireApiKey);
app.use('/api/lane-bindings/', requireApiKey);
app.use('/api/lane-bindings/*', requireApiKey);

async function listBindings(c: Context) {
    const rows = await AppDataSource.query(
        `SELECT route_type, route_key, lane_name
         FROM lane_routing
         WHERE is_active = true
         ORDER BY route_type, route_key`,
    );
    return c.json({ data: rows });
}

async function upsertBinding(c: Context) {
    const body = await c.req.json<{
        route_type?: string;
        route_key?: string;
        lane_name?: string;
    }>();
    const routeType = body.route_type?.trim();
    const routeKey = body.route_key?.trim();
    const laneName = body.lane_name?.trim();
    if (!routeType || !routeKey || !laneName) {
        return c.json({ error: 'route_type, route_key, lane_name are required' }, 400);
    }

    await AppDataSource.query(
        `INSERT INTO lane_routing (route_type, route_key, lane_name, is_active)
         VALUES ($1, $2, $3, true)
         ON CONFLICT (route_type, route_key) WHERE is_active = true
         DO UPDATE SET lane_name = $3`,
        [routeType, routeKey, laneName],
    );
    getLaneRouter().clearCache();
    return c.json({ ok: true, route_type: routeType, route_key: routeKey, lane_name: laneName });
}

async function deleteBinding(c: Context) {
    const routeType = c.req.query('type')?.trim();
    const routeKey = c.req.query('key')?.trim();
    if (!routeType || !routeKey) {
        return c.json({ error: 'type and key query params are required' }, 400);
    }

    await AppDataSource.query(
        'UPDATE lane_routing SET is_active = false WHERE route_type = $1 AND route_key = $2',
        [routeType, routeKey],
    );
    getLaneRouter().clearCache();
    return c.json({ ok: true });
}

app.get('/api/lane-bindings', listBindings);
app.get('/api/lane-bindings/', listBindings);
app.post('/api/lane-bindings', upsertBinding);
app.post('/api/lane-bindings/', upsertBinding);
app.delete('/api/lane-bindings', deleteBinding);
app.delete('/api/lane-bindings/', deleteBinding);

export default app;
