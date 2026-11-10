import type { MongoConfig } from "@inner/shared/mongo";

type Env = Record<string, string | undefined>;

function requireEnv(env: Env, name: string): string {
  const value = env[name];
  if (!value) {
    throw new Error(`${name} is required`);
  }
  return value;
}

function parseOptionalInt(env: Env, name: string, min: number): number | undefined {
  const raw = env[name];
  if (!raw) {
    return undefined;
  }
  const parsed = Number(raw);
  if (!Number.isSafeInteger(parsed) || parsed < min) {
    throw new Error(`${name} must be an integer >= ${min}`);
  }
  return parsed;
}

function hostHasPort(host: string): boolean {
  return host.includes(":");
}

/**
 * worker 唯一的 Mongo 连接：集群内的 chiwei_pixiv 库（图片目录、下载队列、翻译表、作者节流）。
 *
 * 这个连接是必需的，没有开关；配置缺失或格式错误在启动时直接报错。
 *
 * socketTimeoutMS 默认 60s：driver 默认是 0（无限等待），网络抖动产生半开连接时
 * 已发出的命令会永久挂起，下载 consumer 的取任务循环会静默卡死（2026-06-27 prod 实例）。
 * 注意这是 socket 不活动超时而非操作总时长，且 driver 的 retryReads/retryWrites
 * 可能重试一次，逻辑操作可超过 60s 才抛错——但不会再无限挂。
 * PIXIV_MONGO_SOCKET_TIMEOUT_MS=0 是显式关闭开关（回到 driver 默认无限等待）。
 */
export function loadPixivMongoConfig(env: Env = process.env): MongoConfig {
  const host = requireEnv(env, "PIXIV_MONGO_HOST");
  const port = parseOptionalInt(env, "PIXIV_MONGO_PORT", 1);

  return {
    host,
    port: hostHasPort(host) ? undefined : port,
    database: env.PIXIV_MONGO_DATABASE || "chiwei_pixiv",
    username: env.PIXIV_MONGO_USERNAME,
    password: env.PIXIV_MONGO_PASSWORD,
    authSource: env.PIXIV_MONGO_AUTH_SOURCE || "admin",
    connectTimeoutMS: parseOptionalInt(env, "PIXIV_MONGO_CONNECT_TIMEOUT_MS", 1) ?? 2000,
    socketTimeoutMS: parseOptionalInt(env, "PIXIV_MONGO_SOCKET_TIMEOUT_MS", 0) ?? 60000,
  };
}
