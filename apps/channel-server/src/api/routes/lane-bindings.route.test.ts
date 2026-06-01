import { describe, it, expect, beforeEach, mock } from 'bun:test';

const queries: Array<{ sql: string; params?: unknown[] }> = [];
let clearCount = 0;

mock.module('@ormconfig', () => ({
    default: {
        query: async (sql: string, params?: unknown[]) => {
            queries.push({ sql, params });
            if (sql.includes('SELECT route_type')) {
                return [{ route_type: 'bot', route_key: 'akao', lane_name: 'ppe-a' }];
            }
            return [];
        },
        getRepository: () => ({
            findOne: async () => null,
            find: async () => [],
            save: async <T>(value: T) => value,
            create: <T>(value: T) => value,
            update: async () => ({ affected: 0 }),
        }),
        createEntityManager: () => ({}),
    },
}));

mock.module('@integrations/lane-router-runtime', () => ({
    getLaneRouter: () => ({
        clearCache: () => {
            clearCount += 1;
        },
    }),
}));

const { default: app } = await import('./lane-bindings.route');

describe('lane bindings route', () => {
    beforeEach(() => {
        process.env.PAAS_TOKEN = 'paas-token';
        queries.length = 0;
        clearCount = 0;
    });

    it('rejects missing API key', async () => {
        const res = await app.request('/api/lane-bindings');
        expect(res.status).toBe(401);
    });

    it('lists active bindings from channel-server', async () => {
        const res = await app.request('/api/lane-bindings/', {
            headers: { 'X-API-Key': 'paas-token' },
        });
        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({
            data: [{ route_type: 'bot', route_key: 'akao', lane_name: 'ppe-a' }],
        });
        expect(queries[0].sql).toContain('FROM lane_routing');
    });

    it('upserts binding and clears lane router cache', async () => {
        const res = await app.request('/api/lane-bindings/', {
            method: 'POST',
            headers: { 'X-API-Key': 'paas-token', 'Content-Type': 'application/json' },
            body: JSON.stringify({
                route_type: 'bot',
                route_key: 'akao',
                lane_name: 'ppe-a',
            }),
        });
        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({
            ok: true,
            route_type: 'bot',
            route_key: 'akao',
            lane_name: 'ppe-a',
        });
        expect(queries[0].sql).toContain('ON CONFLICT');
        expect(queries[0].params).toEqual(['bot', 'akao', 'ppe-a']);
        expect(clearCount).toBe(1);
    });

    it('soft-deletes binding and clears lane router cache', async () => {
        const res = await app.request('/api/lane-bindings/?type=bot&key=akao', {
            method: 'DELETE',
            headers: { 'X-API-Key': 'paas-token' },
        });
        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ ok: true });
        expect(queries[0].sql).toContain('UPDATE lane_routing SET is_active = false');
        expect(queries[0].params).toEqual(['bot', 'akao']);
        expect(clearCount).toBe(1);
    });
});
