// Redis 接入的唯一一套 API：RedisClient 类 + 进程级单例 getRedisClient()。
// 服务侧不要再各自包一层模块级自由函数 —— 那会变成第二套 API，新服务只能复制
// 或适配。需要新命令就往 RedisClient 上加方法。

// redis-client
export type { RedisConfig } from './redis-client';
export {
    createDefaultRedisConfig,
    RedisClient,
    getRedisClient,
    resetRedisClient,
} from './redis-client';

// redis-lock
export type { LockOptions, RedisLockOperations } from './redis-lock';
export { createRedisLock } from './redis-lock';

// cache-decorator
export type { CacheOptions, RedisCacheOperations } from './cache-decorator';
export { createCacheDecorator, clearLocalCache, getLocalCacheSize } from './cache-decorator';

// 绑定到单例的开箱即用缓存装饰器（拼装只在包内做一次）
export { cache } from './bound';
