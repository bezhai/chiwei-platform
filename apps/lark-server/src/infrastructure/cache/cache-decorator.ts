import { get as redisGet, setWithExpire as redisSetWithExpire } from './redis-client';
import {
    createCacheDecorator,
    CacheOptions,
    RedisCacheOperations,
    clearLocalCache,
    getLocalCacheSize,
} from '@inner/shared';

// Create Redis cache operations using local redis functions
const redisOps: RedisCacheOperations = {
    get: redisGet,
    setWithExpire: async (key: string, value: string, seconds: number) => {
        const result = await redisSetWithExpire(key, value, seconds);
        return result as 'OK';
    },
};

/**
 * 通用缓存装饰器，支持本地缓存和 Redis 缓存
 * @param options 缓存选项
 */
export function cache(options: CacheOptions) {
    return createCacheDecorator(options, redisOps);
}
