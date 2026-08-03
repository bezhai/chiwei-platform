/**
 * 进程配置：全部来自 env，缺一项就在启动期炸。
 *
 * 连接串本身不在这里读 —— PG 由 @inner/shared/persistence 从 POSTGRES_* 组装，
 * Redis 由 @inner/shared/cache 组装，MQ 由 @inner/shared/mq 读 RABBITMQ_URL。
 * 这里做的是**存在性检查**：那三处都在用到的时候才读 env，缺 key 的症状是第一条
 * 消息进来时从 pg / amqplib 底层抛一个跟配置无关的报错。新服务在 PaaS 上要重新
 * 配一遍 App envs 和 ConfigBundle，漏一个 key 是最可能发生的事，必须在 Pod 起来
 * 的那一刻就说清楚缺谁。
 */

export interface LarkServiceConfig {
    port: number;
}

type Env = Record<string, string | undefined>;

// 本服务真正会用到的后端连接所需的 key。谁在用见上方注释。
const REQUIRED_ENV = [
    'POSTGRES_HOST',
    'POSTGRES_USER',
    'POSTGRES_PASSWORD',
    'POSTGRES_DB',
    'REDIS_HOST',
    'RABBITMQ_URL',
    // 飞书原始报文的审计集合（lark_event）落在 mongo。用户名密码是可选的（本地
    // 无鉴权也能连），主机名不是。
    'MONGO_HOST',
] as const;

export function loadConfig(env: Env = process.env): LarkServiceConfig {
    const missing = REQUIRED_ENV.filter((key) => !env[key]);
    if (missing.length > 0) {
        throw new Error(`lark-service: missing required env ${missing.join(', ')}`);
    }

    const port = Number(env.PORT ?? 3000);
    if (!Number.isInteger(port) || port <= 0) {
        throw new Error(`lark-service: PORT must be a positive integer, got ${env.PORT}`);
    }

    return { port };
}
