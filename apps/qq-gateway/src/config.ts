/**
 * 网关配置：全部从 env 读，必填项缺失 fail-fast。
 *
 * QQ 凭据不在这里——按 botName 从 bot_config.credentials 读（见 qq/credentials.ts），
 * 所以这里只放定位凭据所需的 botName 和 postgres 连接信息。
 */

export interface RedisConfig {
    host: string;
    port: number;
    password?: string;
}

export interface PostgresConfig {
    host: string;
    port: number;
    user: string;
    password: string;
    db: string;
}

export interface QQGatewayConfig {
    port: number;
    botName: string;
    innerSecret: string;
    registryUrl: string;
    channelServerService: string;
    channelServerInboundPath: string;
    redis: RedisConfig;
    postgres: PostgresConfig;
    windowMs: number;
    maxReplies: number;
}

type Env = Record<string, string | undefined>;

function required(env: Env, key: string): string {
    const v = env[key];
    if (v === undefined || v === '') {
        throw new Error(`qq-gateway: missing required env ${key}`);
    }
    return v;
}

export function loadConfig(env: Env = process.env): QQGatewayConfig {
    const redis: RedisConfig = {
        host: env.REDIS_HOST || 'localhost',
        port: parseInt(env.REDIS_PORT || '6379', 10),
    };
    if (env.REDIS_PASSWORD) redis.password = env.REDIS_PASSWORD;

    const postgres: PostgresConfig = {
        host: required(env, 'POSTGRES_HOST'),
        port: parseInt(env.POSTGRES_PORT || '5432', 10),
        user: required(env, 'POSTGRES_USER'),
        password: required(env, 'POSTGRES_PASSWORD'),
        db: required(env, 'POSTGRES_DB'),
    };

    return {
        port: parseInt(env.PORT || '3000', 10),
        botName: required(env, 'QQ_BOT_NAME'),
        innerSecret: required(env, 'INNER_HTTP_SECRET'),
        registryUrl: env.REGISTRY_URL || 'http://lite-registry:8080',
        channelServerService: env.CHANNEL_SERVER_SERVICE || 'channel-server',
        channelServerInboundPath: env.CHANNEL_SERVER_INBOUND_PATH || '/api/internal/qq/inbound',
        redis,
        postgres,
        windowMs: parseInt(env.QQ_PASSIVE_WINDOW_MS || String(60 * 60 * 1000), 10),
        maxReplies: parseInt(env.QQ_PASSIVE_MAX_REPLIES || '4', 10),
    };
}
