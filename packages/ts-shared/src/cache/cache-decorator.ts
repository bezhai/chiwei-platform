/**
 * Cache options interface
 */
export interface CacheOptions {
    type: 'local' | 'redis';
    ttl: number; // seconds
}

/**
 * Redis cache operations interface
 */
export interface RedisCacheOperations {
    get(key: string): Promise<string | null>;
    setWithExpire(key: string, value: string, seconds: number): Promise<'OK'>;
}

// Simple local cache implementation
const localCache = new Map<string, { value: unknown; expire: number }>();

function genCacheKey(fnName: string, args: unknown[]): string {
    return `${fnName}:${JSON.stringify(args)}`;
}

/**
 * Create a cache decorator
 * @param options Cache options
 * @param redisOps Optional Redis operations (required for redis type)
 */
export function createCacheDecorator(options: CacheOptions, redisOps?: RedisCacheOperations) {
    const { type, ttl } = options;

    return function (
        target: unknown,
        propertyKey: string,
        descriptor: PropertyDescriptor,
    ) {
        const originalMethod = descriptor.value;

        descriptor.value = async function (...args: unknown[]) {
            const key = genCacheKey(propertyKey, args);
            const now = Date.now();

            if (type === 'local') {
                const cached = localCache.get(key);
                if (cached && cached.expire > now) {
                    return cached.value;
                }
                const result = await originalMethod.apply(this, args);
                localCache.set(key, { value: result, expire: now + ttl * 1000 });
                return result;
            } else if (type === 'redis') {
                if (!redisOps) {
                    throw new Error('Redis operations required for redis cache type');
                }
                const cached = await redisOps.get(key);
                if (cached !== null) {
                    try {
                        return JSON.parse(cached);
                    } catch {
                        return cached;
                    }
                }
                const result = await originalMethod.apply(this, args);
                await redisOps.setWithExpire(key, JSON.stringify(result), ttl);
                return result;
            } else {
                return await originalMethod.apply(this, args);
            }
        };

        return descriptor;
    };
}

/**
 * Clear local cache
 */
export function clearLocalCache(): void {
    localCache.clear();
}

/**
 * Get local cache size
 */
export function getLocalCacheSize(): number {
    return localCache.size;
}
