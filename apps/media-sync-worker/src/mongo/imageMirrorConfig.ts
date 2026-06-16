type Env = Record<string, string | undefined>;

export interface PixivImageMirrorMongoConfig {
    host: string;
    port?: number;
    username?: string;
    password?: string;
    database: string;
    authSource?: string;
    connectTimeoutMS?: number;
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

function hostHasPort(host: string): boolean {
    return host.includes(':');
}

export function loadPixivImageMirrorMongoConfig(env: Env = process.env): PixivImageMirrorMongoConfig | null {
    if (!isEnabled(env.PIXIV_IMAGE_MIRROR_MONGO_ENABLED)) {
        return null;
    }

    const host = requireEnv(env, 'PIXIV_IMAGE_MIRROR_MONGO_HOST');
    const database = env.PIXIV_IMAGE_MIRROR_MONGO_DATABASE || 'chiwei_pixiv';
    const configuredPort = parseOptionalPositiveInt(env, 'PIXIV_IMAGE_MIRROR_MONGO_PORT');

    return {
        host,
        port: hostHasPort(host) ? undefined : configuredPort,
        database,
        username: env.PIXIV_IMAGE_MIRROR_MONGO_USERNAME,
        password: env.PIXIV_IMAGE_MIRROR_MONGO_PASSWORD,
        authSource: env.PIXIV_IMAGE_MIRROR_MONGO_AUTH_SOURCE || 'admin',
        connectTimeoutMS: parseOptionalPositiveInt(env, 'PIXIV_IMAGE_MIRROR_MONGO_CONNECT_TIMEOUT_MS') ?? 2000,
    };
}
