import { setWithExpire, evalScript } from './redis-client';
import { createRedisLock, LockOptions, RedisLockOperations } from '@inner/shared';

// Create Redis lock decorator using local redis functions
const redisOps: RedisLockOperations = {
    setWithExpire: async (key: string, value: string, seconds: number) => {
        const result = await setWithExpire(key, value, seconds);
        return result as 'OK';
    },
    evalScript,
};

/**
 * Redis distributed lock decorator
 * Uses the local redis-client functions
 */
export function RedisLock(options: LockOptions = {}) {
    return createRedisLock(redisOps, options);
}
