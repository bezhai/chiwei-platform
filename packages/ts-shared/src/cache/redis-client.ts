import Redis from 'ioredis';

/**
 * Redis configuration options
 */
export interface RedisConfig {
    host: string;
    port: number;
    password?: string;
    retryStrategy?: (times: number) => number;
}

/**
 * Create default Redis configuration from environment variables
 */
export function createDefaultRedisConfig(): RedisConfig {
    return {
        host: process.env.REDIS_HOST || 'localhost',
        port: parseInt(process.env.REDIS_PORT || '6379', 10),
        password: process.env.REDIS_PASSWORD,
        retryStrategy: (times: number) => {
            const delay = Math.min(times * 50, 2000);
            return delay;
        },
    };
}

/**
 * Redis client wrapper class
 */
export class RedisClient {
    private redis: Redis;
    private redisSub: Redis;
    private channelHandlers: Map<string, Set<(channel: string, message: string) => void>> = new Map();

    constructor(config: RedisConfig = createDefaultRedisConfig()) {
        this.redis = new Redis(config);
        this.redisSub = new Redis(config);

        this.redis.on('connect', () => {
            console.info('Connected to Redis');
        });

        this.redis.on('error', (err: Error) => {
            console.error('Redis connection error:', err);
        });

        this.redisSub.on('connect', () => {
            console.info('Connected to Redis Subscriber');
        });

        this.redisSub.on('error', (err: Error) => {
            console.error('Redis Subscriber connection error:', err);
        });

        this.redisSub.on('message', (channel: string, message: string) => {
            const handlers = this.channelHandlers.get(channel);
            if (handlers) {
                for (const handler of handlers) {
                    try {
                        handler(channel, message);
                    } catch (error) {
                        console.error(`Error in Redis message handler for channel ${channel}:`, error);
                    }
                }
            }
        });
    }

    async incr(key: string): Promise<number> {
        return this.redis.incr(key);
    }

    async set(key: string, value: string): Promise<'OK'> {
        return this.redis.set(key, value);
    }

    async setWithExpire(key: string, value: string, seconds: number): Promise<'OK'> {
        return this.redis.set(key, value, 'EX', seconds);
    }

    async get(key: string): Promise<string | null> {
        return this.redis.get(key);
    }

    async publish(channel: string, message: string): Promise<number> {
        return this.redis.publish(channel, message);
    }

    async subscribe(
        channel: string,
        handler: (channel: string, message: string) => void,
    ): Promise<void> {
        if (!this.channelHandlers.has(channel)) {
            this.channelHandlers.set(channel, new Set());
            await this.redisSub.subscribe(channel);
        }
        const handlers = this.channelHandlers.get(channel)!;
        handlers.add(handler);
    }

    async unsubscribe(
        channel: string,
        handler?: (channel: string, message: string) => void,
    ): Promise<void> {
        if (!this.channelHandlers.has(channel)) {
            return;
        }

        const handlers = this.channelHandlers.get(channel)!;

        if (handler) {
            handlers.delete(handler);
        } else {
            handlers.clear();
        }

        if (handlers.size === 0) {
            this.channelHandlers.delete(channel);
            await this.redisSub.unsubscribe(channel);
        }
    }

    async psubscribe(
        pattern: string,
        handler: (pattern: string, channel: string, message: string) => void,
    ): Promise<void> {
        this.redisSub.removeAllListeners('pmessage');
        await this.redisSub.psubscribe(pattern);
        this.redisSub.on('pmessage', handler);
        console.info(`Redis: subscribed to pattern ${pattern}`);
    }

    async punsubscribe(pattern: string): Promise<void> {
        await this.redisSub.punsubscribe(pattern);
    }

    async close(): Promise<void> {
        await this.redis.quit();
        await this.redisSub.quit();
    }

    async xadd(key: string, id: string, ...fieldValues: string[]): Promise<string> {
        // @ts-ignore
        const result = await this.redis.xadd(key, id, ...fieldValues);
        return result as string;
    }

    async xread(
        ...args: (string | number)[]
    ): Promise<Array<[string, Array<[string, string[]]>]> | null> {
        // @ts-ignore
        return (await this.redis.xread(...args)) as Array<[string, Array<[string, string[]]>]> | null;
    }

    async xdel(key: string, id: string): Promise<number> {
        return this.redis.xdel(key, id) as Promise<number>;
    }

    async xgroup(
        operation: string,
        key: string,
        groupName: string,
        id: string,
        mkstream = false,
    ): Promise<string> {
        const args: string[] = [operation, key, groupName, id];
        if (mkstream) {
            args.push('MKSTREAM');
        }
        // @ts-ignore
        const result = await this.redis.xgroup(...args);
        return result as string;
    }

    async xreadgroup(
        groupName: string,
        consumerName: string,
        ...args: (string | number)[]
    ): Promise<Array<[string, Array<[string, string[]]>]> | null> {
        // @ts-ignore
        return (await this.redis.xreadgroup('GROUP', groupName, consumerName, ...args)) as Array<
            [string, Array<[string, string[]]>]
        > | null;
    }

    async xack(key: string, groupName: string, id: string): Promise<number> {
        return this.redis.xack(key, groupName, id) as Promise<number>;
    }

    async del(...keys: string[]): Promise<number> {
        return this.redis.del(...keys);
    }

    async setNx(key: string, value: string, seconds?: number): Promise<'OK' | null> {
        if (seconds) {
            const result = await this.redis.call('SET', key, value, 'EX', seconds, 'NX');
            return result as 'OK' | null;
        } else {
            return this.redis.setnx(key, value).then((result) => (result === 1 ? 'OK' : null));
        }
    }

    async evalScript(
        script: string,
        numKeys: number,
        ...keysAndArgs: (string | number)[]
    ): Promise<unknown> {
        return this.redis.eval(script, numKeys, ...keysAndArgs);
    }

    async exists(key: string): Promise<number> {
        return this.redis.exists(key);
    }

    // ==================== Hash 操作 ====================

    /**
     * 获取 Hash 中指定字段的值
     * @param key Hash 键名
     * @param field 字段名
     * @returns 字段值，不存在返回 null
     */
    async hget(key: string, field: string): Promise<string | null> {
        return this.redis.hget(key, field);
    }

    /**
     * 设置 Hash 中指定字段的值
     * @param key Hash 键名
     * @param field 字段名
     * @param value 字段值
     * @returns 1 表示新字段，0 表示更新已有字段
     */
    async hset(key: string, field: string, value: string): Promise<number> {
        return this.redis.hset(key, field, value);
    }

    /**
     * 批量设置 Hash 字段
     * @param key Hash 键名
     * @param fieldValues 字段值对象
     */
    async hmset(key: string, fieldValues: Record<string, string>): Promise<'OK'> {
        return this.redis.hmset(key, fieldValues);
    }

    /**
     * 批量获取 Hash 字段
     * @param key Hash 键名
     * @param fields 字段名数组
     * @returns 字段值数组
     */
    async hmget(key: string, ...fields: string[]): Promise<(string | null)[]> {
        return this.redis.hmget(key, ...fields);
    }

    /**
     * 获取 Hash 中所有字段和值
     * @param key Hash 键名
     * @returns 字段值对象
     */
    async hgetall(key: string): Promise<Record<string, string>> {
        return this.redis.hgetall(key);
    }

    /**
     * 删除 Hash 中的字段
     * @param key Hash 键名
     * @param fields 要删除的字段名
     * @returns 删除的字段数量
     */
    async hdel(key: string, ...fields: string[]): Promise<number> {
        return this.redis.hdel(key, ...fields);
    }

    /**
     * 检查 Hash 中字段是否存在
     * @param key Hash 键名
     * @param field 字段名
     * @returns 1 存在，0 不存在
     */
    async hexists(key: string, field: string): Promise<number> {
        return this.redis.hexists(key, field);
    }

    /**
     * 获取 Hash 中所有字段名
     * @param key Hash 键名
     * @returns 字段名数组
     */
    async hkeys(key: string): Promise<string[]> {
        return this.redis.hkeys(key);
    }

    /**
     * 获取 Hash 中所有值
     * @param key Hash 键名
     * @returns 值数组
     */
    async hvals(key: string): Promise<string[]> {
        return this.redis.hvals(key);
    }

    /**
     * 获取 Hash 中字段数量
     * @param key Hash 键名
     * @returns 字段数量
     */
    async hlen(key: string): Promise<number> {
        return this.redis.hlen(key);
    }

    /**
     * Hash 字段值增加整数
     * @param key Hash 键名
     * @param field 字段名
     * @param increment 增量
     * @returns 增加后的值
     */
    async hincrby(key: string, field: string, increment: number): Promise<number> {
        return this.redis.hincrby(key, field, increment);
    }

    // ==================== Set 操作 ====================

    /**
     * 向 Set 添加成员
     * @param key Set 键名
     * @param members 要添加的成员
     * @returns 添加的新成员数量
     */
    async sadd(key: string, ...members: string[]): Promise<number> {
        return this.redis.sadd(key, ...members);
    }

    /**
     * 获取 Set 中所有成员
     * @param key Set 键名
     * @returns 成员数组
     */
    async smembers(key: string): Promise<string[]> {
        return this.redis.smembers(key);
    }

    /**
     * 检查成员是否在 Set 中
     * @param key Set 键名
     * @param member 成员
     * @returns 1 存在，0 不存在
     */
    async sismember(key: string, member: string): Promise<number> {
        return this.redis.sismember(key, member);
    }

    /**
     * 从 Set 中移除成员
     * @param key Set 键名
     * @param members 要移除的成员
     * @returns 移除的成员数量
     */
    async srem(key: string, ...members: string[]): Promise<number> {
        return this.redis.srem(key, ...members);
    }

    /**
     * 获取 Set 中成员数量
     * @param key Set 键名
     * @returns 成员数量
     */
    async scard(key: string): Promise<number> {
        return this.redis.scard(key);
    }

    /**
     * 随机获取 Set 中的成员
     * @param key Set 键名
     * @param count 获取数量（可选）
     * @returns 随机成员或成员数组
     */
    async srandmember(key: string, count?: number): Promise<string | string[] | null> {
        if (count !== undefined) {
            return this.redis.srandmember(key, count);
        }
        return this.redis.srandmember(key);
    }

    /**
     * 随机弹出 Set 中的成员
     * @param key Set 键名
     * @param count 弹出数量（可选）
     * @returns 弹出的成员
     */
    async spop(key: string, count?: number): Promise<string | string[] | null> {
        if (count !== undefined) {
            return this.redis.spop(key, count);
        }
        return this.redis.spop(key);
    }

    // ==================== 通用操作 ====================

    /**
     * 设置键的过期时间
     * @param key 键名
     * @param seconds 过期时间（秒）
     * @returns 1 成功，0 键不存在
     */
    async expire(key: string, seconds: number): Promise<number> {
        return this.redis.expire(key, seconds);
    }

    /**
     * 设置键在指定时间戳过期
     * @param key 键名
     * @param timestamp Unix 时间戳（秒）
     * @returns 1 成功，0 键不存在
     */
    async expireat(key: string, timestamp: number): Promise<number> {
        return this.redis.expireat(key, timestamp);
    }

    /**
     * 获取键的剩余过期时间
     * @param key 键名
     * @returns 剩余秒数，-1 表示永不过期，-2 表示键不存在
     */
    async ttl(key: string): Promise<number> {
        return this.redis.ttl(key);
    }

    /**
     * 移除键的过期时间
     * @param key 键名
     * @returns 1 成功，0 键不存在或没有过期时间
     */
    async persist(key: string): Promise<number> {
        return this.redis.persist(key);
    }

    /**
     * 获取键的类型
     * @param key 键名
     * @returns 类型字符串：string, list, set, zset, hash, stream
     */
    async type(key: string): Promise<string> {
        return this.redis.type(key);
    }

    /**
     * 查找匹配模式的键
     * @param pattern 匹配模式
     * @returns 匹配的键数组
     */
    async keys(pattern: string): Promise<string[]> {
        return this.redis.keys(pattern);
    }

    /**
     * 重命名键
     * @param key 原键名
     * @param newKey 新键名
     */
    async rename(key: string, newKey: string): Promise<'OK'> {
        return this.redis.rename(key, newKey);
    }

    /**
     * 获取原生 Redis 客户端实例
     * 用于执行未封装的命令
     */
    getNativeClient(): Redis {
        return this.redis;
    }
}

// Default singleton instance
let defaultInstance: RedisClient | null = null;

export function getRedisClient(config?: RedisConfig): RedisClient {
    if (!defaultInstance) {
        defaultInstance = new RedisClient(config);
    }
    return defaultInstance;
}

export function resetRedisClient(): void {
    if (defaultInstance) {
        defaultInstance.close();
        defaultInstance = null;
    }
}
