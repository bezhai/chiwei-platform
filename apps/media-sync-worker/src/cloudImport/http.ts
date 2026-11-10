/**
 * 一次性导入的 admin 接口，挂在 worker 现有的 HTTP server（tagger 回调 server）上。T6 删除。
 *
 * - POST /internal/media-sync/cloud-import：后台启动一次导入，立即返回 run id；已有一次在跑时返回 409。
 * - GET  /internal/media-sync/cloud-import：正在跑的那次的进度，和最近几次运行的事件。
 * - GET  /internal/media-sync/cloud-settings：只读地返回云端 Redis 的三个名单和 version。
 *
 * 鉴权沿用平台服务间调用的约定：Authorization: Bearer <INNER_HTTP_SECRET>。
 * 云端连接只在处理请求时建立；云端 env 缺失时接口返回 503，不影响 worker 启动。
 */
import { randomUUID } from 'node:crypto';
import { pixivCollections } from '../mongo/client';
import type { RequestHandler } from '../tagger/callbackServer';
import { readCloudSettings } from './cloudSettings';
import { cloudConnectors, loadCloudSourceConfig, type CloudConnectors, type CloudSourceConfig } from './cloudSource';
import {
    listRecentRuns,
    runCloudImport,
    type ClusterTarget,
    type MergeCollection,
    type RunLog,
    type StepStats,
} from './importer';

const ADMIN_PREFIX = '/internal/media-sync/';
export const CLOUD_IMPORT_PATH = `${ADMIN_PREFIX}cloud-import`;
export const CLOUD_SETTINGS_PATH = `${ADMIN_PREFIX}cloud-settings`;

const RUNS_COLLECTION = 'pixiv_cloud_import_runs';
const BATCH_SIZE = 500;
const DEFAULT_RUN_LIMIT = 5;
const MAX_RUN_LIMIT = 50;

export interface CloudImportHandlerDeps {
    /** 每次请求时读取 INNER_HTTP_SECRET 和云端 bundle 的 key */
    env: Record<string, string | undefined>;
    connectors: CloudConnectors;
    cluster: () => Promise<{ target: ClusterTarget; runLog: RunLog }>;
    newRunId: () => string;
    now: () => Date;
    batchSize: number;
}

interface ActiveRun {
    run_id: string;
    started_at: Date;
    progress: StepStats | null;
}

export function createCloudImportHandler(deps: CloudImportHandlerDeps): RequestHandler {
    // 进程内只允许一次导入在跑；worker 是单副本
    let active: ActiveRun | null = null;

    const startImport = async (): Promise<Response> => {
        if (active) {
            return json({ error: 'a cloud import is already running', run_id: active.run_id }, 409);
        }
        const loaded = loadCloudSourceConfig(deps.env);
        if (!loaded.ok) {
            return json({ error: loaded.error }, 503);
        }
        const run: ActiveRun = { run_id: deps.newRunId(), started_at: deps.now(), progress: null };
        active = run;

        let cluster: { target: ClusterTarget; runLog: RunLog };
        try {
            cluster = await deps.cluster();
        } catch (err) {
            active = null;
            return json({ error: `cluster Mongo unavailable: ${errorMessage(err)}` }, 500);
        }

        void runCloudImport({
            runId: run.run_id,
            openSource: () => openCloudSource(deps.connectors, loaded.config),
            target: cluster.target,
            runLog: cluster.runLog,
            batchSize: deps.batchSize,
            now: deps.now,
            onProgress: (stats) => {
                run.progress = stats;
            },
        })
            .catch((err) => {
                console.error(`Cloud import aborted: run_id=${run.run_id} error=${errorMessage(err)}`);
            })
            .finally(() => {
                active = null;
            });

        return json({ run_id: run.run_id }, 202);
    };

    const listRuns = async (url: URL): Promise<Response> => {
        const limit = parseLimit(url.searchParams.get('limit'));
        if (limit === null) {
            return json({ error: `limit must be an integer between 1 and ${MAX_RUN_LIMIT}` }, 400);
        }
        try {
            const { runLog } = await deps.cluster();
            return json({ active, runs: await listRecentRuns(runLog, limit) });
        } catch (err) {
            return json({ error: errorMessage(err) }, 500);
        }
    };

    const readSettings = async (): Promise<Response> => {
        const loaded = loadCloudSourceConfig(deps.env);
        if (!loaded.ok) {
            return json({ error: loaded.error }, 503);
        }
        const redis = deps.connectors.redis(loaded.config.redis);
        try {
            return json(await readCloudSettings(redis.client));
        } catch (err) {
            return json({ error: `cloud Redis read failed: ${errorMessage(err)}` }, 502);
        } finally {
            await redis.close().catch((err) => {
                console.warn(`Cloud settings: closing cloud Redis failed: ${errorMessage(err)}`);
            });
        }
    };

    return async (request: Request): Promise<Response> => {
        const url = new URL(request.url);
        if (!isAuthorized(request, deps.env.INNER_HTTP_SECRET)) {
            console.warn(`Media sync admin request rejected: reason=unauthorized method=${request.method} path=${url.pathname}`);
            return json({ error: 'unauthorized' }, 401);
        }
        if (url.pathname === CLOUD_IMPORT_PATH && request.method === 'POST') {
            return startImport();
        }
        if (url.pathname === CLOUD_IMPORT_PATH && request.method === 'GET') {
            return listRuns(url);
        }
        if (url.pathname === CLOUD_SETTINGS_PATH && request.method === 'GET') {
            return readSettings();
        }
        return json({ error: 'not found' }, 404);
    };
}

/** /internal/media-sync/ 下的请求交给导入接口，其余交给 next（tagger 回调和 /health） */
export function routeCloudImport(cloudImport: RequestHandler, next: RequestHandler): RequestHandler {
    return (request: Request) =>
        new URL(request.url).pathname.startsWith(ADMIN_PREFIX) ? cloudImport(request) : next(request);
}

/** 生产接线：构造时不读云端 env、不建立任何连接 */
export function cloudImportHandlerFromEnv(env: Record<string, string | undefined> = process.env): RequestHandler {
    return createCloudImportHandler({
        env,
        connectors: cloudConnectors,
        cluster: clusterStores,
        newRunId: () => randomUUID(),
        now: () => new Date(),
        batchSize: BATCH_SIZE,
    });
}

async function clusterStores(): Promise<{ target: ClusterTarget; runLog: RunLog }> {
    const collections = await pixivCollections();
    const db = collections.images.getNativeCollection().db;
    // 集合名以 mongo/client.ts 的绑定为准，这里只换成无类型的 driver 集合
    const untyped = (collection: { getNativeCollection(): { collectionName: string } }): MergeCollection =>
        db.collection(collection.getNativeCollection().collectionName);
    return {
        target: {
            images: untyped(collections.images),
            downloadTasks: untyped(collections.downloadTasks),
            tagTranslations: untyped(collections.tagTranslations),
            authorDownloads: untyped(collections.authorDownloads),
        },
        runLog: db.collection(RUNS_COLLECTION),
    };
}

async function openCloudSource(connectors: CloudConnectors, config: CloudSourceConfig) {
    const mongo = await connectors.mongo(config.mongo);
    const redis = connectors.redis(config.redis);
    return {
        source: { ...mongo.collections, redis: redis.client },
        close: async () => {
            const results = await Promise.allSettled([mongo.close(), redis.close()]);
            for (const result of results) {
                if (result.status === 'rejected') {
                    console.warn(`Cloud import: closing a cloud connection failed: ${errorMessage(result.reason)}`);
                }
            }
        },
    };
}

/**
 * 与 @inner/shared/middleware 的 bearerAuthMiddleware 同一约定（Bearer 前缀、字符串全等）。
 * secret 未配置（含空串）时一律拒绝。Fetch 会去掉 header 尾部空白，空 token 本来到不了比较这一步，
 * 这里显式拒绝，不依赖这一点。
 */
function isAuthorized(request: Request, secret: string | undefined): boolean {
    if (!secret) {
        return false;
    }
    const header = request.headers.get('authorization');
    if (!header || !header.startsWith('Bearer ')) {
        return false;
    }
    return header.substring('Bearer '.length) === secret;
}

function parseLimit(raw: string | null): number | null {
    if (raw === null) {
        return DEFAULT_RUN_LIMIT;
    }
    if (!/^\d+$/.test(raw)) {
        return null;
    }
    const limit = Number(raw);
    return limit >= 1 && limit <= MAX_RUN_LIMIT ? limit : null;
}

function errorMessage(err: unknown): string {
    return err instanceof Error ? err.message : String(err);
}

function json(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
        status,
        headers: { 'content-type': 'application/json' },
    });
}
