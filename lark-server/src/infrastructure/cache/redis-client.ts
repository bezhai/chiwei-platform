import Redis from 'ioredis';

// Redis配置选项
const redisConfig = {
    host: process.env.REDIS_HOST || 'localhost',
    port: parseInt(process.env.REDIS_PORT || '6379', 10),
    password: process.env.REDIS_PASSWORD,
    retryStrategy: (times: number) => {
        const delay = Math.min(times * 50, 2000);
        return delay;
    },
};

// 创建主Redis客户端
const redis = new Redis(redisConfig);

// 单独创建一个用于订阅的Redis客户端
// 因为在订阅模式下，客户端不能执行其他命令
const redisSub = new Redis(redisConfig);

redis.on('connect', () => {
    console.info(`Connected to Redis`);
});

redis.on('error', (err: Error) => {
    console.error('Redis connection error:', err);
});

redisSub.on('connect', () => {
    console.info(`Connected to Redis Subscriber`);
});

redisSub.on('error', (err: Error) => {
    console.error('Redis Subscriber connection error:', err);
});

// 存储频道订阅的处理函数
const channelHandlers: Map<string, Set<(channel: string, message: string) => void>> = new Map();

// 当订阅客户端收到消息时
redisSub.on('message', (channel: string, message: string) => {
    const handlers = channelHandlers.get(channel);
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

export async function incr(key: string): Promise<number> {
    return redis.incr(key);
}

export async function set(key: string, value: string) {
    return redis.set(key, value);
}

export async function setWithExpire(key: string, value: string, seconds: number) {
    return redis.set(key, value, 'EX', seconds);
}

export async function get(key: string): Promise<string | null> {
    return redis.get(key);
}

/**
 * 发布消息到Redis频道
 * @param channel 频道名
 * @param message 消息内容
 * @returns 接收消息的客户端数量
 */
export async function publish(channel: string, message: string): Promise<number> {
    return redis.publish(channel, message);
}

/**
 * 订阅Redis频道
 * @param channel 频道名
 * @param handler 消息处理函数
 */
export async function subscribe(
    channel: string,
    handler: (channel: string, message: string) => void,
): Promise<void> {
    // 如果是第一次订阅此频道，需要在Redis中订阅
    if (!channelHandlers.has(channel)) {
        channelHandlers.set(channel, new Set());
        await redisSub.subscribe(channel);
    }

    // 添加处理函数
    const handlers = channelHandlers.get(channel)!;
    handlers.add(handler);
}

/**
 * 取消订阅Redis频道
 * @param channel 频道名
 * @param handler 可选，指定要取消的处理函数。如果不指定，取消所有处理函数
 */
export async function unsubscribe(
    channel: string,
    handler?: (channel: string, message: string) => void,
): Promise<void> {
    if (!channelHandlers.has(channel)) {
        return;
    }

    const handlers = channelHandlers.get(channel)!;

    if (handler) {
        // 移除特定处理函数
        handlers.delete(handler);
    } else {
        // 移除所有处理函数
        handlers.clear();
    }

    // 如果没有更多处理函数，在Redis中取消订阅
    if (handlers.size === 0) {
        channelHandlers.delete(channel);
        await redisSub.unsubscribe(channel);
    }
}

/**
 * 使用模式订阅多个频道
 * @param pattern 频道模式，如 event:*
 * @param handler 消息处理函数
 */
export async function psubscribe(
    pattern: string,
    handler: (pattern: string, channel: string, message: string) => void,
): Promise<void> {
    // 先取消现有的pmessage监听器，避免重复监听
    redisSub.removeAllListeners('pmessage');

    // 订阅模式
    await redisSub.psubscribe(pattern);

    // 注册新的处理函数
    redisSub.on('pmessage', handler);

    console.info(`Redis: 已订阅模式 ${pattern}`);
}

/**
 * 取消模式订阅
 * @param pattern 频道模式
 */
export async function punsubscribe(pattern: string): Promise<void> {
    await redisSub.punsubscribe(pattern);
}

/**
 * 关闭Redis连接
 */
export async function close(): Promise<void> {
    await redis.quit();
    await redisSub.quit();
}

/**
 * 向Redis Stream添加消息
 * @param key Stream的键名
 * @param id 消息ID，通常使用'*'让Redis自动生成
 * @param fieldValues 键值对数组，表示消息的字段和值 [field1, value1, field2, value2, ...]
 * @returns 返回添加消息的ID
 */
export async function xadd(key: string, id: string, ...fieldValues: string[]): Promise<string> {
    // @ts-ignore
    const result = await redis.xadd(key, id, ...fieldValues);
    return result as string;
}

/**
 * 从Redis Stream读取消息
 * @param args 命令参数，如 ['BLOCK', 5000, 'STREAMS', 'mystream', '0']
 * @returns 返回读取的消息数组，格式为 [[streamName, [[messageId, [field1, value1, ...]], ...]]]
 */
export async function xread(
    ...args: (string | number)[]
): Promise<Array<[string, Array<[string, string[]]>]> | null> {
    // @ts-ignore
    return (await redis.xread(...args)) as Array<[string, Array<[string, string[]]>]> | null;
}

/**
 * 从Redis Stream中删除指定ID的消息
 * @param key Stream的键名
 * @param id 要删除的消息ID
 * @returns 返回删除的消息数量
 */
export async function xdel(key: string, id: string): Promise<number> {
    return redis.xdel(key, id) as Promise<number>;
}

/**
 * 创建或修改消费者组
 * @param key Stream的键名
 * @param groupName 消费者组名称
 * @param id 起始ID，通常使用'$'（只消费新消息）或'0'（消费所有消息）
 * @param mkstream 如果为true且stream不存在，则创建stream
 * @returns 操作成功返回'OK'
 */
export async function xgroup(
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
    const result = await redis.xgroup(...args);
    return result as string;
}

/**
 * 通过消费者组读取消息
 * @param groupName 消费者组名称
 * @param consumerName 消费者名称
 * @param args 命令参数
 * @returns 返回读取的消息数组
 */
export async function xreadgroup(
    groupName: string,
    consumerName: string,
    ...args: (string | number)[]
): Promise<Array<[string, Array<[string, string[]]>]> | null> {
    // @ts-ignore
    return (await redis.xreadgroup('GROUP', groupName, consumerName, ...args)) as Array<
        [string, Array<[string, string[]]>]
    > | null;
}

/**
 * 确认消息已经处理
 * @param key Stream的键名
 * @param groupName 消费者组名称
 * @param id 消息ID
 * @returns 返回确认的消息数量
 */
export async function xack(key: string, groupName: string, id: string): Promise<number> {
    return redis.xack(key, groupName, id) as Promise<number>;
}

/**
 * 删除一个或多个键
 * @param keys 要删除的键名数组
 * @returns 删除的键数量
 */
export async function del(...keys: string[]): Promise<number> {
    return redis.del(...keys);
}

/**
 * SET IF NOT EXISTS - 只有当key不存在时才设置值
 * @param key 键名
 * @param value 值
 * @param seconds 过期时间（秒）
 * @returns 如果成功设置返回'OK'，如果键已存在返回null
 */
export async function setNx(key: string, value: string, seconds?: number): Promise<'OK' | null> {
    if (seconds) {
        // 使用 SET key value EX seconds NX 原子操作
        const result = await redis.call('SET', key, value, 'EX', seconds, 'NX');
        return result as 'OK' | null;
    } else {
        return redis.setnx(key, value).then(result => result === 1 ? 'OK' : null);
    }
}

/**
 * 执行 Lua 脚本
 * @param script Lua 脚本内容
 * @param numKeys 脚本中使用的键的数量
 * @param keysAndArgs 键名和参数数组
 * @returns 脚本执行结果
 */
export async function evalScript(script: string, numKeys: number, ...keysAndArgs: (string | number)[]): Promise<any> {
    return redis.eval(script, numKeys, ...keysAndArgs);
}

/**
 * 检查键是否存在
 * @param key 键名
 * @returns 存在返回1，不存在返回0
 */
export async function exists(key: string): Promise<number> {
    return redis.exists(key);
}
