import { describe, expect, it } from 'bun:test';
import { loadCloudSourceConfig } from './cloudSource';

const COMPLETE = {
    MONGO_HOST: 'cloud-mongo',
    MONGO_PORT: '27017',
    MONGO_INITDB_ROOT_USERNAME: 'root',
    MONGO_INITDB_ROOT_PASSWORD: 'mongo-pass',
    REDIS_HOST: 'cloud-redis',
    REDIS_PORT: '6380',
    REDIS_PASSWORD: 'redis-pass',
};

describe('loadCloudSourceConfig', () => {
    it('reads the media-sync-worker-cloud-data bundle keys into the cloud chiwei database and Redis', () => {
        expect(loadCloudSourceConfig(COMPLETE)).toEqual({
            ok: true,
            config: {
                mongo: {
                    host: 'cloud-mongo',
                    port: 27017,
                    username: 'root',
                    password: 'mongo-pass',
                    database: 'chiwei',
                    authSource: 'admin',
                    connectTimeoutMS: 10000,
                    socketTimeoutMS: 60000,
                },
                redis: { host: 'cloud-redis', port: 6380, password: 'redis-pass' },
            },
        });
    });

    it('lists every missing or empty key', () => {
        const result = loadCloudSourceConfig({ ...COMPLETE, MONGO_HOST: '', REDIS_PASSWORD: undefined });

        expect(result.ok).toBe(false);
        if (!result.ok) {
            expect(result.error).toContain('MONGO_HOST');
            expect(result.error).toContain('REDIS_PASSWORD');
            expect(result.error).not.toContain('REDIS_HOST');
        }
    });

    it('rejects a port that is not a positive integer', () => {
        const result = loadCloudSourceConfig({ ...COMPLETE, REDIS_PORT: '63x9' });

        expect(result).toEqual({ ok: false, error: expect.stringContaining('REDIS_PORT') });
    });

    it('ignores MONGO_PORT when MONGO_HOST already carries a port, like the old worker config', () => {
        const result = loadCloudSourceConfig({ ...COMPLETE, MONGO_HOST: 'cloud-mongo:27018' });

        expect(result.ok && result.config.mongo.port).toBeUndefined();
        expect(result.ok && result.config.mongo.host).toBe('cloud-mongo:27018');
    });
});
