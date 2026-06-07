import type { MongoConfig } from '@inner/shared/mongo';

type Env = Record<string, string | undefined>;

export interface TaggerTriggerConfig {
    entryUrl: string;
    apiToken: string;
    callbackUrl: string;
    batchSize: number;
    submitTimeoutMs: number;
    submitRetries: number;
    workerIdleDelayMs: number;
    retryDelayMs: number;
    processingTimeoutMs: number;
    maxAttempts: number;
}

export interface TaggerCallbackServerConfig {
    port: number;
    authToken: string;
}

function isEnabled(value: string | undefined): boolean {
    return value === '1' || value?.toLowerCase() === 'true';
}

function requireEnv(env: Env, name: string): string {
    const value = env[name];
    if (!value) {
        throw new Error(`${name} is required`);
    }
    return value;
}

function parseOptionalPositiveInt(env: Env, name: string): number | undefined {
    const raw = env[name];
    if (!raw) {
        return undefined;
    }
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed <= 0) {
        throw new Error(`${name} must be a positive integer`);
    }
    return parsed;
}

function parseOptionalNonNegativeInt(env: Env, name: string): number | undefined {
    const raw = env[name];
    if (!raw) {
        return undefined;
    }
    const parsed = Number.parseInt(raw, 10);
    if (!Number.isFinite(parsed) || parsed < 0) {
        throw new Error(`${name} must be a non-negative integer`);
    }
    return parsed;
}

function hostHasPort(host: string): boolean {
    return host.includes(':');
}

export function loadTaggerResultMongoConfig(env: Env = process.env): MongoConfig | null {
    if (!isEnabled(env.TAGGER_RESULT_MONGO_ENABLED)) {
        return null;
    }

    const host = requireEnv(env, 'TAGGER_RESULT_MONGO_HOST');
    const database = requireEnv(env, 'TAGGER_RESULT_MONGO_DATABASE');
    const configuredPort = parseOptionalPositiveInt(env, 'TAGGER_RESULT_MONGO_PORT');

    return {
        host,
        port: hostHasPort(host) ? undefined : configuredPort,
        database,
        username: env.TAGGER_RESULT_MONGO_USERNAME,
        password: env.TAGGER_RESULT_MONGO_PASSWORD,
        authSource: env.TAGGER_RESULT_MONGO_AUTH_SOURCE || 'admin',
        connectTimeoutMS: parseOptionalPositiveInt(env, 'TAGGER_RESULT_MONGO_CONNECT_TIMEOUT_MS') ?? 2000,
    };
}

export function loadTaggerTriggerConfig(env: Env = process.env): TaggerTriggerConfig | null {
    if (!isEnabled(env.TAGGER_TRIGGER_ENABLED)) {
        return null;
    }

    return {
        entryUrl: requireEnv(env, 'TAGGER_ENTRY_URL'),
        apiToken: requireEnv(env, 'TAGGER_API_TOKEN'),
        callbackUrl: requireEnv(env, 'TAGGER_CALLBACK_URL'),
        batchSize: parseOptionalPositiveInt(env, 'TAGGER_SUBMIT_BATCH_SIZE') ?? 1,
        submitTimeoutMs: parseOptionalPositiveInt(env, 'TAGGER_SUBMIT_TIMEOUT_MS') ?? 10000,
        submitRetries: parseOptionalNonNegativeInt(env, 'TAGGER_SUBMIT_RETRIES') ?? 2,
        workerIdleDelayMs: parseOptionalPositiveInt(env, 'TAGGER_TRIGGER_WORKER_IDLE_DELAY_MS') ?? 5000,
        retryDelayMs: parseOptionalPositiveInt(env, 'TAGGER_TRIGGER_RETRY_DELAY_MS') ?? 60000,
        processingTimeoutMs: parseOptionalPositiveInt(env, 'TAGGER_TRIGGER_PROCESSING_TIMEOUT_MS') ?? 600000,
        maxAttempts: parseOptionalPositiveInt(env, 'TAGGER_TRIGGER_MAX_ATTEMPTS') ?? 5,
    };
}

export function loadTaggerCallbackServerConfig(env: Env = process.env): TaggerCallbackServerConfig | null {
    if (!isEnabled(env.TAGGER_CALLBACK_SERVER_ENABLED)) {
        return null;
    }

    return {
        port: parseOptionalPositiveInt(env, 'TAGGER_CALLBACK_PORT')
            ?? parseOptionalPositiveInt(env, 'PORT')
            ?? 3000,
        authToken: requireEnv(env, 'TAGGER_CALLBACK_AUTH_TOKEN'),
    };
}
