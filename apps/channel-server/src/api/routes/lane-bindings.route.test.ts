import { afterAll, describe, it, expect, beforeEach, mock } from 'bun:test';

const queries: Array<{ sql: string; params?: unknown[] }> = [];
let clearCount = 0;

// `@ormconfig` 和 `ormconfig` 是同一个文件（tsconfig path alias），mock 它等于
// 把全进程的 DataSource 换成这个只认 query 的桩。先抓真身、afterAll 放回。
const realOrmconfig = { ...(await import('@ormconfig')) };
mock.module('@ormconfig', () => ({
    ...realOrmconfig,
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

// bun 的 mock.module 是**整模块替换**（且进程级全局、mock.restore() 不撤销）：
// 只写 getLaneBindingResolver 会让同模块的 LANE_ROUTING_TABLE 直接消失，被测
// 路由 import 它时报 "Export named ... not found"。所以先抓真身、只覆盖要桩的
// 那一个导出。（这个坑真发生过：全量跑因为别的文件先加载了真身而侥幸绿，单跑红。）
const realLaneBinding = { ...(await import('@inner/shared/lane-binding')) };
mock.module('@inner/shared/lane-binding', () => ({
    ...realLaneBinding,
    getLaneBindingResolver: () => ({
        clearCache: () => {
            clearCount += 1;
        },
    }),
}));

// 两个 mock 都必须还原：bun 的 mock.module 注册表是进程级的，mock.restore() 不撤销，
// 留着就会让后续加载的生产代码拿到假 DataSource / 缺 resolveLane 的 resolver。
afterAll(() => {
    mock.module('@ormconfig', () => realOrmconfig);
    mock.module('@inner/shared/lane-binding', () => realLaneBinding);
});

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
