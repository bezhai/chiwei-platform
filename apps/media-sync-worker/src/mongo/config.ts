import { MongoConfig } from "@inner/shared/mongo";

type EnvLike = Record<string, string | undefined>;

function parseEnvInt(env: EnvLike, name: string, defaultValue: number, allowZero = false): number {
  const raw = env[name];
  if (!raw) {
    return defaultValue;
  }
  const parsed = Number.parseInt(raw, 10);
  const min = allowZero ? 0 : 1;
  return Number.isFinite(parsed) && parsed >= min ? parsed : defaultValue;
}

function hostHasPort(host: string): boolean {
  return host.includes(":");
}

/**
 * 从环境变量构造 worker 主 Mongo 连接配置。
 *
 * socketTimeoutMS 默认 60s：driver 默认是 0（无限等待），网络抖动产生半开连接时
 * 已发出的命令会永久挂起，下载 consumer 的取任务循环会静默卡死（2026-06-27 prod 实例）。
 * 注意这是 socket 不活动超时而非操作总时长，且 driver 的 retryReads/retryWrites
 * 可能重试一次，逻辑操作可超过 60s 才抛错——但不会再无限挂。
 * MONGO_SOCKET_TIMEOUT_MS=0 是显式关闭开关（回到 driver 默认无限等待），误杀慢操作时可用。
 */
export function loadMongoConfigFromEnv(env: EnvLike = process.env): MongoConfig {
  const host = env.MONGO_HOST || "mongo";

  return {
    host,
    port: hostHasPort(host) ? undefined : parseEnvInt(env, "MONGO_PORT", 27017),
    username: env.MONGO_INITDB_ROOT_USERNAME,
    password: env.MONGO_INITDB_ROOT_PASSWORD,
    database: "chiwei",
    authSource: "admin",
    connectTimeoutMS: parseEnvInt(env, "MONGO_CONNECT_TIMEOUT_MS", 10000),
    socketTimeoutMS: parseEnvInt(env, "MONGO_SOCKET_TIMEOUT_MS", 60000, true),
  };
}
