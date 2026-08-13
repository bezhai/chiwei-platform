// 绑定到进程级 Redis 单例的缓存装饰器。
//
// createCacheDecorator 是"要什么 redis 操作"的工厂，各服务如果各自拿
// getRedisClient() 去拼一遍 ops，那份拼装就会在每个服务里重复一份 —— 正是收敛
// Redis API 时要消灭的东西。所以拼装只在这里做一次。

import {
    createCacheDecorator,
    type CacheOptions,
    type RedisCacheOperations,
} from './cache-decorator';
import { getRedisClient } from './redis-client';

// 延迟取 client：装饰器在模块加载期求值，这一刻不能就去建连接。
const redisOps: RedisCacheOperations = {
    get: (key) => getRedisClient().get(key),
    setWithExpire: (key, value, seconds) => getRedisClient().setWithExpire(key, value, seconds),
};

/** 本地 + Redis 两级缓存装饰器，走进程级 Redis 单例。 */
export function cache(options: CacheOptions) {
    return createCacheDecorator(options, redisOps);
}
