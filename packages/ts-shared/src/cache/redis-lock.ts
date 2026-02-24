import { randomBytes } from 'crypto';

/**
 * Lock options interface
 */
export interface LockOptions {
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    key?: string | ((...args: any[]) => string);
    ttl?: number;
    timeout?: number;
    retryInterval?: number;
}

/**
 * Redis operations interface for lock
 */
export interface RedisLockOperations {
    setWithExpire(key: string, value: string, seconds: number): Promise<'OK'>;
    evalScript(script: string, numKeys: number, ...keysAndArgs: (string | number)[]): Promise<unknown>;
}

const UNLOCK_SCRIPT = `
if redis.call("get", KEYS[1]) == ARGV[1] then
  return redis.call("del", KEYS[1])
else
  return 0
end`;

/**
 * Create a Redis lock decorator
 * @param redisOps Redis operations (setWithExpire, evalScript)
 * @param options Lock options
 */
export function createRedisLock(redisOps: RedisLockOperations, options: LockOptions = {}) {
    return function (
        target: unknown,
        propertyKey: string,
        descriptor: PropertyDescriptor,
    ) {
        const originalMethod = descriptor.value;

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        descriptor.value = async function (...args: any[]) {
            // Generate lock key dynamically
            let lockKey: string;
            if (typeof options.key === 'function') {
                lockKey = options.key(...args);
            } else {
                const targetName = (target as { constructor: { name: string } }).constructor.name;
                lockKey = options.key || `lock:${targetName}:${propertyKey}`;
            }

            const lockValue = randomBytes(16).toString('hex');
            const ttl = options.ttl || 30;
            const timeout = options.timeout || 10000;
            const retryInterval = options.retryInterval || 100;

            const acquireLock = async (): Promise<boolean> => {
                const result = await redisOps.setWithExpire(lockKey, lockValue, ttl);
                return result === 'OK';
            };

            const releaseLock = async (): Promise<void> => {
                await redisOps.evalScript(UNLOCK_SCRIPT, 1, lockKey, lockValue);
            };

            const waitForLock = async (): Promise<void> => {
                const startTime = Date.now();
                while (Date.now() - startTime < timeout) {
                    if (await acquireLock()) return;
                    await new Promise((resolve) => setTimeout(resolve, retryInterval));
                }
                throw new Error(`Failed to acquire lock for ${lockKey} within ${timeout}ms`);
            };

            await waitForLock();

            try {
                return await originalMethod.apply(this, args);
            } finally {
                await releaseLock();
            }
        };

        return descriptor;
    };
}
