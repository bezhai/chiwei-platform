import { describe, expect, it } from 'bun:test';
import { loadMongoConfigFromEnv } from './config';

describe('loadMongoConfigFromEnv', () => {
    it('defaults socketTimeoutMS to 60s so a half-open connection cannot hang the consumer forever', () => {
        const config = loadMongoConfigFromEnv({});

        expect(config).toEqual({
            host: 'mongo',
            port: 27017,
            username: undefined,
            password: undefined,
            database: 'chiwei',
            authSource: 'admin',
            connectTimeoutMS: 10000,
            socketTimeoutMS: 60000,
        });
    });

    it('allows env overrides and ignores invalid values', () => {
        const config = loadMongoConfigFromEnv({
            MONGO_HOST: 'other-host',
            MONGO_PORT: '27018',
            MONGO_INITDB_ROOT_USERNAME: 'user',
            MONGO_INITDB_ROOT_PASSWORD: 'pass',
            MONGO_CONNECT_TIMEOUT_MS: '5000',
            MONGO_SOCKET_TIMEOUT_MS: '30000',
        });

        expect(config.host).toBe('other-host');
        expect(config.port).toBe(27018);
        expect(config.username).toBe('user');
        expect(config.password).toBe('pass');
        expect(config.connectTimeoutMS).toBe(5000);
        expect(config.socketTimeoutMS).toBe(30000);

        const invalid = loadMongoConfigFromEnv({ MONGO_SOCKET_TIMEOUT_MS: 'abc' });
        expect(invalid.socketTimeoutMS).toBe(60000);
    });

    it('treats MONGO_SOCKET_TIMEOUT_MS=0 as an explicit off switch (driver default: wait forever)', () => {
        const config = loadMongoConfigFromEnv({ MONGO_SOCKET_TIMEOUT_MS: '0' });

        expect(config.socketTimeoutMS).toBe(0);
    });

    it('leaves port undefined when the host already contains one', () => {
        const config = loadMongoConfigFromEnv({ MONGO_HOST: 'mongo-host:27019' });

        expect(config.host).toBe('mongo-host:27019');
        expect(config.port).toBeUndefined();
    });
});
