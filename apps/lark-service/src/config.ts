/**
 * 进程配置：全部来自 env，缺一项就在启动期炸。
 *
 * 连接串本身不在这里读 —— PG 由 @inner/shared/persistence 从 POSTGRES_* 组装，
 * Redis 由 @inner/shared/cache 组装，MQ 由 @inner/shared/mq 读 RABBITMQ_URL。
 * 这里做的是**存在性检查**：那三处都在用到的时候才读 env，缺 key 的症状是第一条
 * 消息进来时从 pg / amqplib 底层抛一个跟配置无关的报错。新服务在 PaaS 上要重新
 * 配一遍 App envs 和 ConfigBundle，漏一个 key 是最可能发生的事，必须在 Pod 起来
 * 的那一刻就说清楚缺谁。
 *
 * 这个镜像产出**两个** Deployment，各自要的 env 不完全一样（见下面两个 load 函数）。
 * 两份清单从同一个 BACKEND_ENV 出发，不各写各的 —— 各写各的迟早会漂。
 */

export interface LarkServiceConfig {
    port: number;
}

export interface LarkOutboundConfig {
    metricsPort: number;
}

type Env = Record<string, string | undefined>;

/** 两个进程都要连的后端。谁在用见文件头注释。 */
const BACKEND_ENV = [
    'POSTGRES_HOST',
    'POSTGRES_USER',
    'POSTGRES_PASSWORD',
    'POSTGRES_DB',
    'REDIS_HOST',
    'RABBITMQ_URL',
] as const;

/**
 * 飞书专属业务（Task D 那四批）要的下游凭据与地址。**只有入口进程用得上** —— 指令、
 * 入站附件管线、定时任务、卡片回调全挂在入站那一侧，出站进程一个都不碰。
 *
 * 之所以现在就列全、而不是等各自那批落地再补：**它们缺了都不报错**，症状是功能静默
 * 消失，而新服务在 PaaS 上要重新配一遍 App envs 和 ConfigBundle，一份完整的清单比
 * 分四次补更不容易漏。
 *
 *   - `INNER_HTTP_SECRET`（D1）附件管线调 tool-service 的内网口令。缺了发出的是
 *     `Bearer undefined`，tool-service 401 —— 而管线是 fire-and-forget，入站照常
 *     工作，只是 TOS 里再也不落新附件，`read_book` 之类稳定读不到东西。
 *   - `MINIO_ENDPOINT` / `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY`（D2）本地 pixiv 图源。
 *     这三个是唯一自己会 fail-closed 的，但也要等到第一次发图才炸。
 *   - `MEME_HOST` / `MEME_PORT`（D4）meme 服务。缺了请求打到
 *     `undefined:undefined/memes/list`。
 *   - `AI_PROVIDER_ADMIN_KEY`（D4）查余额用的管理密钥。缺了拿 401。
 *
 * 有默认值的那些**不列**，漏配不会静默出错：`REGISTRY_URL`（默认
 * `http://lite-registry:8080`）、`MINIO_PORT` / `MINIO_BUCKET` / `MINIO_USE_SSL`、
 * `PIXIV_IMAGE_MONGO_*` 一族（注意它连的是另一个 mongo 实例，不复用 `MONGO_HOST`）。
 */
const LARK_BUSINESS_ENV = [
    'INNER_HTTP_SECRET',
    'MINIO_ENDPOINT',
    'MINIO_ACCESS_KEY',
    'MINIO_SECRET_KEY',
    'MEME_HOST',
    'MEME_PORT',
    'AI_PROVIDER_ADMIN_KEY',
] as const;

/**
 * 入口进程另外要的。
 *
 * 飞书原始报文的审计集合（lark_event）落在 mongo。用户名密码是可选的（本地无鉴权
 * 也能连），主机名不是。
 */
const INGRESS_ENV = [...BACKEND_ENV, 'MONGO_HOST', ...LARK_BUSINESS_ENV] as const;

function requireEnv(env: Env, keys: readonly string[]): void {
    const missing = keys.filter((key) => !env[key]);
    if (missing.length > 0) {
        throw new Error(`lark-service: missing required env ${missing.join(', ')}`);
    }
}

function port(env: Env, key: string, fallback: number): number {
    const raw = env[key];
    const value = Number(raw ?? fallback);
    if (!Number.isInteger(value) || value <= 0) {
        throw new Error(`lark-service: ${key} must be a positive integer, got ${raw}`);
    }
    return value;
}

/** 入口进程（webhook + 长连 + 泳道信封）。 */
export function loadConfig(env: Env = process.env): LarkServiceConfig {
    requireEnv(env, INGRESS_ENV);
    return { port: port(env, 'PORT', 3000) };
}

/**
 * 出站进程（消费飞书的 chat_response）。
 *
 * **不要 MONGO_HOST**：它不是飞书事件的入口，原始报文在入口那一侧第一次进来时就
 * 已经记过了。多要一个 key 只是多一个起不来的理由。
 *
 * 它不开 HTTP 服务，只开一个 metrics 端口 —— 出站是竞争消费，判断它活得好不好靠
 * 队列积压和处理时延，不靠一个健康检查接口。
 */
export function loadOutboundConfig(env: Env = process.env): LarkOutboundConfig {
    requireEnv(env, BACKEND_ENV);
    return { metricsPort: port(env, 'METRICS_PORT', 9091) };
}
