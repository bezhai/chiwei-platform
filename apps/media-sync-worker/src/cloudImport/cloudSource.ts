/**
 * 云机器上的 Mongo（库 chiwei）和 Redis：只有一次性导入和读云端设置时才连接，用完即关。
 * 配置来自 ConfigBundle media-sync-worker-cloud-data，T6 和这个目录一起删除。
 */
import Redis from 'ioredis';
import { MongoService, type MongoConfig } from '@inner/shared/mongo';
import type { CloudSource } from './importer';

export interface CloudRedisConfig {
    host: string;
    port: number;
    password: string;
}

export interface CloudSourceConfig {
    mongo: MongoConfig;
    redis: CloudRedisConfig;
}

export type CloudSourceConfigResult = { ok: true; config: CloudSourceConfig } | { ok: false; error: string };

const BUNDLE_KEYS = [
    'MONGO_HOST',
    'MONGO_PORT',
    'MONGO_INITDB_ROOT_USERNAME',
    'MONGO_INITDB_ROOT_PASSWORD',
    'REDIS_HOST',
    'REDIS_PORT',
    'REDIS_PASSWORD',
] as const;

const CONNECT_TIMEOUT_MS = 10_000;
const SOCKET_TIMEOUT_MS = 60_000;
const REDIS_COMMAND_TIMEOUT_MS = 10_000;

/**
 * 读 bundle 的 7 个 key。缺失时返回错误而不是抛出：worker 启动不读这些 key，
 * 只有调用导入或读设置的接口时才检查，接口据此返回明确的错误。
 */
export function loadCloudSourceConfig(env: Record<string, string | undefined>): CloudSourceConfigResult {
    const missing = BUNDLE_KEYS.filter((key) => !env[key]);
    if (missing.length > 0) {
        return {
            ok: false,
            error: `cloud data env (ConfigBundle media-sync-worker-cloud-data) is incomplete: missing ${missing.join(', ')}`,
        };
    }
    const value = (key: (typeof BUNDLE_KEYS)[number]) => env[key] as string;

    const mongoPort = parsePort(value('MONGO_PORT'));
    const redisPort = parsePort(value('REDIS_PORT'));
    const invalid = [
        ...(mongoPort === null ? ['MONGO_PORT'] : []),
        ...(redisPort === null ? ['REDIS_PORT'] : []),
    ];
    if (invalid.length > 0) {
        return { ok: false, error: `${invalid.join(', ')} must be a port number` };
    }

    const mongoHost = value('MONGO_HOST');
    return {
        ok: true,
        config: {
            mongo: {
                host: mongoHost,
                // 和旧 worker 一样：MONGO_HOST 自带端口时不再拼 MONGO_PORT
                port: mongoHost.includes(':') ? undefined : mongoPort!,
                username: value('MONGO_INITDB_ROOT_USERNAME'),
                password: value('MONGO_INITDB_ROOT_PASSWORD'),
                database: 'chiwei',
                authSource: 'admin',
                connectTimeoutMS: CONNECT_TIMEOUT_MS,
                socketTimeoutMS: SOCKET_TIMEOUT_MS,
            },
            redis: { host: value('REDIS_HOST'), port: redisPort!, password: value('REDIS_PASSWORD') },
        },
    };
}

function parsePort(raw: string): number | null {
    if (!/^\d+$/.test(raw)) {
        return null;
    }
    const port = Number(raw);
    return port > 0 && port <= 65535 ? port : null;
}

export interface CloudRedis {
    hgetall(key: string): Promise<Record<string, string>>;
    smembers(key: string): Promise<string[]>;
    get(key: string): Promise<string | null>;
}

export interface CloudConnectors {
    mongo(config: MongoConfig): Promise<{
        collections: Pick<CloudSource, 'imgMap' | 'downloadTask' | 'transMap'>;
        close(): Promise<void>;
    }>;
    redis(config: CloudRedisConfig): { client: CloudRedis; close(): Promise<void> };
}

export const cloudConnectors: CloudConnectors = {
    async mongo(config) {
        const service = new MongoService(config);
        await service.initialize();
        return {
            collections: {
                imgMap: service.getNativeCollection('img_map'),
                downloadTask: service.getNativeCollection('download_task'),
                transMap: service.getNativeCollection('trans_map'),
            },
            close: () => service.close(),
        };
    },

    redis(config) {
        // 直接用 ioredis 而不是 @inner/shared 的 RedisClient：RedisConfig.retryStrategy 只能返回数字（一直重连），
        // 还多开一条订阅连接；它的 close() 用 quit，连接没建立且离线队列里还有命令时，quit 也排进队列等重连。
        // 这里限制重连次数，关闭用 disconnect，云机器连不上时命令按 commandTimeout 失败，接口和导入都能结束。
        const client = new Redis({
            host: config.host,
            port: config.port,
            password: config.password,
            lazyConnect: true,
            connectTimeout: CONNECT_TIMEOUT_MS,
            commandTimeout: REDIS_COMMAND_TIMEOUT_MS,
            maxRetriesPerRequest: 1,
            retryStrategy: (times) => (times > 3 ? null : times * 500),
        });
        client.on('error', (err: Error) => {
            console.warn(`Cloud Redis error: ${err.message}`);
        });
        return {
            client,
            close: async () => {
                client.disconnect();
            },
        };
    },
};
