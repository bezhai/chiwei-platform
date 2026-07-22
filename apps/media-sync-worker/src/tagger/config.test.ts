import { describe, expect, it } from 'bun:test';
import {
    loadTaggerCallbackServerConfig,
    loadTaggerResultMongoConfig,
    loadTaggerProjectionConfig,
    loadTaggerTriggerConfig,
} from './config';

describe('loadTaggerResultMongoConfig', () => {
    it('returns null when TAGGER_RESULT_MONGO_ENABLED is not explicitly on', () => {
        const config = loadTaggerResultMongoConfig({
            TAGGER_RESULT_MONGO_HOST: 'local-mongo',
            TAGGER_RESULT_MONGO_DATABASE: 'chiwei_tagger',
        });

        expect(config).toBeNull();
    });

    it('does not fall back to legacy MONGO_* source envs', () => {
        expect(() =>
            loadTaggerResultMongoConfig({
                TAGGER_RESULT_MONGO_ENABLED: 'true',
                MONGO_HOST: 'legacy-source-mongo',
                MONGO_PORT: '27017',
                MONGO_INITDB_ROOT_USERNAME: 'legacy-user',
                MONGO_INITDB_ROOT_PASSWORD: 'legacy-pass',
            })
        ).toThrow('TAGGER_RESULT_MONGO_HOST is required');
    });

    it('parses the explicit result Mongo env set', () => {
        const config = loadTaggerResultMongoConfig({
            TAGGER_RESULT_MONGO_ENABLED: '1',
            TAGGER_RESULT_MONGO_HOST: 'local-mongo',
            TAGGER_RESULT_MONGO_PORT: '27018',
            TAGGER_RESULT_MONGO_DATABASE: 'chiwei_tagger',
            TAGGER_RESULT_MONGO_USERNAME: 'root',
            TAGGER_RESULT_MONGO_PASSWORD: 'secret',
            TAGGER_RESULT_MONGO_AUTH_SOURCE: 'admin',
            TAGGER_RESULT_MONGO_CONNECT_TIMEOUT_MS: '3000',
        });

        expect(config).toEqual({
            host: 'local-mongo',
            port: 27018,
            database: 'chiwei_tagger',
            username: 'root',
            password: 'secret',
            authSource: 'admin',
            connectTimeoutMS: 3000,
        });
    });

    it('omits port when host already contains a port', () => {
        const config = loadTaggerResultMongoConfig({
            TAGGER_RESULT_MONGO_ENABLED: 'true',
            TAGGER_RESULT_MONGO_HOST: 'local-mongo:27017',
            TAGGER_RESULT_MONGO_PORT: '27018',
            TAGGER_RESULT_MONGO_DATABASE: 'chiwei_tagger',
        });

        expect(config?.host).toBe('local-mongo:27017');
        expect(config?.port).toBeUndefined();
    });
});

describe('loadTaggerTriggerConfig', () => {
    it('returns null when TAGGER_TRIGGER_ENABLED is not explicitly on', () => {
        const config = loadTaggerTriggerConfig({
            TAGGER_ENTRY_URL: 'http://tagger-entry:8000',
            TAGGER_API_TOKEN: 'caller-token',
            TAGGER_CALLBACK_URL: 'http://media-sync-worker/internal/tagger/callback',
        });

        expect(config).toBeNull();
    });

    it('requires explicit tagger entry, caller token, and callback URL when enabled', () => {
        expect(() =>
            loadTaggerTriggerConfig({
                TAGGER_TRIGGER_ENABLED: 'true',
            })
        ).toThrow('TAGGER_ENTRY_URL is required');
    });

    it('parses submit options with defaults', () => {
        const config = loadTaggerTriggerConfig({
            TAGGER_TRIGGER_ENABLED: 'true',
            TAGGER_ENTRY_URL: 'http://tagger-entry:8000/',
            TAGGER_API_TOKEN: 'caller-token',
            TAGGER_CALLBACK_URL: 'http://media-sync-worker/internal/tagger/callback',
        });

        expect(config).toEqual({
            entryUrl: 'http://tagger-entry:8000/',
            apiToken: 'caller-token',
            callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
            batchSize: 1,
            submitTimeoutMs: 10000,
            submitRetries: 2,
            workerIdleDelayMs: 5000,
            retryDelayMs: 60000,
            processingTimeoutMs: 600000,
            maxAttempts: 5,
            reconcileAfterMs: 600000,
            reconcileLeaseMs: 60000,
            reconcileRetryDelayMs: 60000,
        });
    });

    it('parses explicit submit sizing and retry settings', () => {
        const config = loadTaggerTriggerConfig({
            TAGGER_TRIGGER_ENABLED: '1',
            TAGGER_ENTRY_URL: 'http://tagger-entry:8000',
            TAGGER_API_TOKEN: 'caller-token',
            TAGGER_CALLBACK_URL: 'http://media-sync-worker/internal/tagger/callback',
            TAGGER_SUBMIT_BATCH_SIZE: '4',
            TAGGER_SUBMIT_TIMEOUT_MS: '30000',
            TAGGER_SUBMIT_RETRIES: '5',
            TAGGER_TRIGGER_WORKER_IDLE_DELAY_MS: '1000',
            TAGGER_TRIGGER_RETRY_DELAY_MS: '15000',
            TAGGER_TRIGGER_PROCESSING_TIMEOUT_MS: '120000',
            TAGGER_TRIGGER_MAX_ATTEMPTS: '9',
            TAGGER_SUBMITTED_RECONCILE_AFTER_MS: '180000',
            TAGGER_SUBMITTED_RECONCILE_LEASE_MS: '45000',
            TAGGER_SUBMITTED_RECONCILE_RETRY_DELAY_MS: '30000',
        });

        expect(config?.batchSize).toBe(4);
        expect(config?.submitTimeoutMs).toBe(30000);
        expect(config?.submitRetries).toBe(5);
        expect(config?.workerIdleDelayMs).toBe(1000);
        expect(config?.retryDelayMs).toBe(15000);
        expect(config?.processingTimeoutMs).toBe(120000);
        expect(config?.maxAttempts).toBe(9);
        expect(config?.reconcileAfterMs).toBe(180000);
        expect(config?.reconcileLeaseMs).toBe(45000);
        expect(config?.reconcileRetryDelayMs).toBe(30000);
    });
});

describe('loadTaggerProjectionConfig', () => {
    it('is explicitly disabled by default even when result Mongo exists', () => {
        expect(loadTaggerProjectionConfig({ TAGGER_RESULT_MONGO_ENABLED: 'true' })).toBeNull();
    });

    it('parses online projection defaults while historical backfill remains off', () => {
        expect(loadTaggerProjectionConfig({ TAGGER_PROJECTION_ENABLED: 'true' })).toEqual({
            batchSize: 4,
            workerIdleDelayMs: 5000,
            retryDelayMs: 60000,
            processingTimeoutMs: 600000,
            includeHistorical: false,
        });
    });

    it('enables historical result backfill only through its independent flag', () => {
        const config = loadTaggerProjectionConfig({
            TAGGER_PROJECTION_ENABLED: '1',
            TAGGER_PROJECTION_BACKFILL_ENABLED: 'true',
            TAGGER_PROJECTION_BATCH_SIZE: '8',
            TAGGER_PROJECTION_WORKER_IDLE_DELAY_MS: '1000',
            TAGGER_PROJECTION_RETRY_DELAY_MS: '20000',
            TAGGER_PROJECTION_PROCESSING_TIMEOUT_MS: '120000',
        });

        expect(config).toEqual({
            batchSize: 8,
            workerIdleDelayMs: 1000,
            retryDelayMs: 20000,
            processingTimeoutMs: 120000,
            includeHistorical: true,
        });
    });
});

describe('loadTaggerCallbackServerConfig', () => {
    it('returns null when callback server is not explicitly enabled', () => {
        expect(loadTaggerCallbackServerConfig({ TAGGER_CALLBACK_AUTH_TOKEN: 'callback-token' })).toBeNull();
    });

    it('requires callback auth token when enabled', () => {
        expect(() =>
            loadTaggerCallbackServerConfig({
                TAGGER_CALLBACK_SERVER_ENABLED: 'true',
            })
        ).toThrow('TAGGER_CALLBACK_AUTH_TOKEN is required');
    });

    it('uses explicit callback port first', () => {
        const config = loadTaggerCallbackServerConfig({
            TAGGER_CALLBACK_SERVER_ENABLED: '1',
            TAGGER_CALLBACK_AUTH_TOKEN: 'callback-token',
            TAGGER_CALLBACK_PORT: '3003',
            PORT: '9999',
        });

        expect(config).toEqual({
            port: 3003,
            authToken: 'callback-token',
        });
    });

    it('falls back to PORT and then 3000 for callback port', () => {
        expect(
            loadTaggerCallbackServerConfig({
                TAGGER_CALLBACK_SERVER_ENABLED: 'true',
                TAGGER_CALLBACK_AUTH_TOKEN: 'callback-token',
                PORT: '3010',
            })?.port
        ).toBe(3010);
        expect(
            loadTaggerCallbackServerConfig({
                TAGGER_CALLBACK_SERVER_ENABLED: 'true',
                TAGGER_CALLBACK_AUTH_TOKEN: 'callback-token',
            })?.port
        ).toBe(3000);
    });
});
