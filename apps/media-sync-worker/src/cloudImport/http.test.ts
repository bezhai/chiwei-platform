import { describe, expect, it } from 'bun:test';
import { ObjectId } from 'mongodb';
import type { RequestHandler } from '../tagger/callbackServer';
import type { CloudConnectors } from './cloudSource';
import {
    CLOUD_IMPORT_PATH,
    CLOUD_SETTINGS_PATH,
    cloudImportHandlerFromEnv,
    createCloudImportHandler,
    routeCloudImport,
    type CloudImportHandlerDeps,
} from './http';
import { MemoryCollection, MemoryRedis } from './memoryStores';

const SECRET = 'inner-secret';
const CLOUD_ENV = {
    MONGO_HOST: 'cloud-mongo',
    MONGO_PORT: '27017',
    MONGO_INITDB_ROOT_USERNAME: 'root',
    MONGO_INITDB_ROOT_PASSWORD: 'mongo-pass',
    REDIS_HOST: 'cloud-redis',
    REDIS_PORT: '6379',
    REDIS_PASSWORD: 'redis-pass',
};

function deferred(): { promise: Promise<void>; resolve: () => void } {
    let resolve!: () => void;
    const promise = new Promise<void>((r) => {
        resolve = r;
    });
    return { promise, resolve };
}

function setup(options: { env?: Record<string, string | undefined>; gate?: Promise<void>; redis?: MemoryRedis } = {}) {
    const calls = { mongoConnect: 0, mongoClose: 0, redisConnect: 0, redisClose: 0, cluster: 0 };
    const redis = options.redis ?? new MemoryRedis({
        hashes: { download_user_dict: { '1': '1700000000' } },
        sets: { ban_illusts: ['300', '1000', '20'], ban_user: ['u2', 'u1'], skip_words: [] },
        strings: { version: 'abc123' },
    });
    const connectors: CloudConnectors = {
        mongo: async () => {
            calls.mongoConnect += 1;
            await options.gate;
            return {
                collections: {
                    imgMap: new MemoryCollection('img_map', [{ _id: new ObjectId(), pixiv_addr: '1_p0.jpg' }]),
                    downloadTask: new MemoryCollection('download_task'),
                    transMap: new MemoryCollection('trans_map'),
                },
                close: async () => {
                    calls.mongoClose += 1;
                },
            };
        },
        redis: () => {
            calls.redisConnect += 1;
            return {
                client: redis,
                close: async () => {
                    calls.redisClose += 1;
                },
            };
        },
    };
    const runLog = new MemoryCollection('pixiv_cloud_import_runs');
    const target = {
        images: new MemoryCollection('pixiv_images'),
        downloadTasks: new MemoryCollection('pixiv_download_tasks'),
        tagTranslations: new MemoryCollection('pixiv_tag_translations'),
        authorDownloads: new MemoryCollection('pixiv_author_downloads'),
    };
    let runCounter = 0;
    const deps: CloudImportHandlerDeps = {
        env: options.env ?? { INNER_HTTP_SECRET: SECRET, ...CLOUD_ENV },
        connectors,
        cluster: async () => {
            calls.cluster += 1;
            return { target, runLog };
        },
        newRunId: () => `run-${++runCounter}`,
        now: () => new Date('2026-11-20T04:00:00.000Z'),
        batchSize: 2,
    };
    return { handler: createCloudImportHandler(deps), calls, runLog, target };
}

function call(handler: RequestHandler, method: string, path: string, token: string | null = SECRET): Promise<Response> {
    const headers: Record<string, string> = {};
    if (token !== null) {
        headers.authorization = `Bearer ${token}`;
    }
    return handler(new Request(`http://worker${path}`, { method, headers }));
}

async function waitUntilIdle(handler: RequestHandler): Promise<void> {
    for (let i = 0; i < 200; i += 1) {
        const body = (await (await call(handler, 'GET', CLOUD_IMPORT_PATH)).json()) as { active: unknown };
        if (body.active === null) {
            return;
        }
        await new Promise((resolve) => setTimeout(resolve, 1));
    }
    throw new Error('import did not finish');
}

describe('cloud import routes: auth', () => {
    const routes: Array<[string, string]> = [
        ['POST', CLOUD_IMPORT_PATH],
        ['GET', CLOUD_IMPORT_PATH],
        ['GET', CLOUD_SETTINGS_PATH],
    ];

    for (const [method, path] of routes) {
        it(`${method} ${path} answers 401 without the inner bearer token and touches nothing`, async () => {
            const { handler, calls } = setup();

            expect((await call(handler, method, path, null)).status).toBe(401);
            expect((await call(handler, method, path, 'wrong')).status).toBe(401);
            expect(
                (await handler(new Request(`http://worker${path}`, { method, headers: { authorization: SECRET } }))).status
            ).toBe(401);
            expect(calls).toEqual({ mongoConnect: 0, mongoClose: 0, redisConnect: 0, redisClose: 0, cluster: 0 });
        });
    }

    it('rejects every token when INNER_HTTP_SECRET is not configured', async () => {
        const { handler, calls } = setup({ env: { ...CLOUD_ENV } });

        expect((await call(handler, 'POST', CLOUD_IMPORT_PATH, '')).status).toBe(401);
        expect((await call(handler, 'POST', CLOUD_IMPORT_PATH, 'undefined')).status).toBe(401);
        expect(calls.mongoConnect).toBe(0);
    });

    it('rejects an empty bearer token when INNER_HTTP_SECRET is set but empty', async () => {
        const { handler, calls } = setup({ env: { INNER_HTTP_SECRET: '', ...CLOUD_ENV } });

        expect((await call(handler, 'POST', CLOUD_IMPORT_PATH, '')).status).toBe(401);
        expect(calls.mongoConnect).toBe(0);
    });
});

describe('POST cloud-import', () => {
    it('starts an import in the background, returns its run id, and closes the cloud connections', async () => {
        const { handler, calls, runLog, target } = setup();

        const res = await call(handler, 'POST', CLOUD_IMPORT_PATH);

        expect(res.status).toBe(202);
        expect(await res.json()).toEqual({ run_id: 'run-1' });
        await waitUntilIdle(handler);
        expect(runLog.snapshot().map((e) => e.event)).toEqual([
            'started',
            'collection_done',
            'collection_done',
            'collection_done',
            'collection_done',
            'finished',
        ]);
        expect(target.images.snapshot()).toHaveLength(1);
        expect(target.authorDownloads.snapshot()).toEqual([{ _id: '1', last_download_at: 1700000000 }]);
        expect(calls).toMatchObject({ mongoConnect: 1, mongoClose: 1, redisConnect: 1, redisClose: 1 });
    });

    it('refuses a second import while one is running, and accepts one after it ends', async () => {
        const gate = deferred();
        const { handler } = setup({ gate: gate.promise });

        expect((await call(handler, 'POST', CLOUD_IMPORT_PATH)).status).toBe(202);
        const second = await call(handler, 'POST', CLOUD_IMPORT_PATH);
        expect(second.status).toBe(409);
        expect(await second.json()).toMatchObject({ run_id: 'run-1' });

        gate.resolve();
        await waitUntilIdle(handler);
        const third = await call(handler, 'POST', CLOUD_IMPORT_PATH);
        expect(third.status).toBe(202);
        expect(await third.json()).toEqual({ run_id: 'run-2' });
        await waitUntilIdle(handler);
    });

    it('answers 503 naming the missing cloud env and connects to nothing', async () => {
        const { handler, calls } = setup({ env: { INNER_HTTP_SECRET: SECRET, MONGO_HOST: 'cloud-mongo' } });

        const res = await call(handler, 'POST', CLOUD_IMPORT_PATH);

        expect(res.status).toBe(503);
        const body = (await res.json()) as { error: string };
        expect(body.error).toContain('REDIS_PASSWORD');
        expect(body.error).toContain('MONGO_INITDB_ROOT_PASSWORD');
        expect(body.error).not.toContain('MONGO_HOST');
        expect(calls).toEqual({ mongoConnect: 0, mongoClose: 0, redisConnect: 0, redisClose: 0, cluster: 0 });
    });
});

describe('GET cloud-import', () => {
    it('shows the running import and, once done, its recorded events', async () => {
        const gate = deferred();
        const { handler } = setup({ gate: gate.promise });
        await call(handler, 'POST', CLOUD_IMPORT_PATH);

        const running = (await (await call(handler, 'GET', CLOUD_IMPORT_PATH)).json()) as any;
        expect(running.active).toMatchObject({ run_id: 'run-1', started_at: '2026-11-20T04:00:00.000Z' });

        gate.resolve();
        await waitUntilIdle(handler);
        const res = await call(handler, 'GET', CLOUD_IMPORT_PATH);
        expect(res.status).toBe(200);
        const done = (await res.json()) as any;
        expect(done.active).toBeNull();
        expect(done.runs).toHaveLength(1);
        expect(done.runs[0].run_id).toBe('run-1');
        expect(done.runs[0].events.at(-1)).toMatchObject({ event: 'finished', at: '2026-11-20T04:00:00.000Z' });
    });

    it('still answers when the cloud env is missing', async () => {
        const { handler } = setup({ env: { INNER_HTTP_SECRET: SECRET } });

        const res = await call(handler, 'GET', CLOUD_IMPORT_PATH);

        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ active: null, runs: [] });
    });
});

describe('GET cloud-settings', () => {
    it('returns the sorted cloud lists, their lengths and version, then closes Redis', async () => {
        const { handler, calls } = setup();

        const res = await call(handler, 'GET', CLOUD_SETTINGS_PATH);

        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({
            ban_illusts: {
                dynamic_config_key: 'pixiv_ban_illusts',
                length: 3,
                json_bytes: '["1000","20","300"]'.length,
                values: ['1000', '20', '300'],
            },
            ban_user: {
                dynamic_config_key: 'pixiv_ban_users',
                length: 2,
                json_bytes: '["u1","u2"]'.length,
                values: ['u1', 'u2'],
            },
            skip_words: { dynamic_config_key: 'pixiv_skip_words', length: 0, json_bytes: 2, values: [] },
            version: { dynamic_config_key: 'pixiv_version', value: 'abc123' },
        });
        expect(calls).toEqual({ mongoConnect: 0, mongoClose: 0, redisConnect: 1, redisClose: 1, cluster: 0 });
    });

    it('answers 502 when Redis fails and still closes the connection', async () => {
        const { handler, calls } = setup({ redis: new MemoryRedis({ hashes: { ban_illusts: { a: '1' } } }) });

        const res = await call(handler, 'GET', CLOUD_SETTINGS_PATH);

        expect(res.status).toBe(502);
        expect(((await res.json()) as { error: string }).error).toContain('WRONGTYPE');
        expect(calls).toMatchObject({ redisConnect: 1, redisClose: 1 });
    });

    it('answers 503 when the cloud env is missing', async () => {
        const { handler, calls } = setup({ env: { INNER_HTTP_SECRET: SECRET } });

        expect((await call(handler, 'GET', CLOUD_SETTINGS_PATH)).status).toBe(503);
        expect(calls.redisConnect).toBe(0);
    });
});

describe('routeCloudImport', () => {
    const next: RequestHandler = async (request) =>
        new Response(`next:${new URL(request.url).pathname}`, { status: 299 });

    it('sends /internal/media-sync/ to the import routes and everything else to the next handler', async () => {
        const { handler } = setup();
        const routed = routeCloudImport(handler, next);

        expect((await call(routed, 'GET', '/health', null)).status).toBe(299);
        expect((await call(routed, 'POST', '/internal/tagger/callback', 'tagger-token')).status).toBe(299);
        expect((await call(routed, 'GET', CLOUD_IMPORT_PATH)).status).toBe(200);
        expect((await call(routed, 'GET', '/internal/media-sync/unknown')).status).toBe(404);
        expect((await call(routed, 'DELETE', CLOUD_IMPORT_PATH)).status).toBe(404);
    });
});

describe('cloudImportHandlerFromEnv', () => {
    it('builds without cloud env or connections, and its routes report the missing env', async () => {
        const handler = cloudImportHandlerFromEnv({ INNER_HTTP_SECRET: SECRET });

        expect((await call(handler, 'POST', CLOUD_IMPORT_PATH)).status).toBe(503);
        expect((await call(handler, 'GET', CLOUD_SETTINGS_PATH)).status).toBe(503);
    });
});
