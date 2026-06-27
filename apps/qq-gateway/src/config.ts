/**
 * 网关配置：全部从 env 读，必填项缺失 fail-fast。
 *
 * QQ 凭据：appId / appSecret 用于刷 access_token；botSecret 用于 webhook Ed25519 验签。
 * QQ 实际中 botSecret == appSecret，故 QQ_BOT_SECRET 缺省回退到 QQ_APP_SECRET。
 */

export interface RedisConfig {
    host: string;
    port: number;
    password?: string;
}

export interface QQGatewayConfig {
    port: number;
    botName: string;
    appId: string;
    appSecret: string;
    botSecret: string;
    webhookPath: string;
    innerSecret: string;
    registryUrl: string;
    channelServerService: string;
    channelServerInboundPath: string;
    redis: RedisConfig;
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
    const appSecret = required(env, 'QQ_APP_SECRET');
    const redis: RedisConfig = {
        host: env.REDIS_HOST || 'localhost',
        port: parseInt(env.REDIS_PORT || '6379', 10),
    };
    if (env.REDIS_PASSWORD) redis.password = env.REDIS_PASSWORD;

    return {
        port: parseInt(env.PORT || '3000', 10),
        botName: required(env, 'QQ_BOT_NAME'),
        appId: required(env, 'QQ_APP_ID'),
        appSecret,
        botSecret: env.QQ_BOT_SECRET || appSecret,
        webhookPath: env.QQ_WEBHOOK_PATH || '/qq/webhook',
        innerSecret: required(env, 'INNER_HTTP_SECRET'),
        registryUrl: env.REGISTRY_URL || 'http://lite-registry:8080',
        channelServerService: env.CHANNEL_SERVER_SERVICE || 'channel-server',
        channelServerInboundPath: env.CHANNEL_SERVER_INBOUND_PATH || '/api/internal/qq/inbound',
        redis,
        windowMs: parseInt(env.QQ_PASSIVE_WINDOW_MS || String(60 * 60 * 1000), 10),
        maxReplies: parseInt(env.QQ_PASSIVE_MAX_REPLIES || '4', 10),
    };
}
