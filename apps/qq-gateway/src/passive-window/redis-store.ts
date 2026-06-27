/**
 * Redis 版被动窗口存储（ioredis）。窗口记录与幂等键落 Redis，网关重启后状态延续。
 *
 * - markIdempotent: SET key '1' PX <ttl> NX —— 原子去重，首次写入成功，已存在返回 null。
 * - setWindow: SET key <json> PX <ttl> —— 窗口记录带过期时间。
 * - getWindow: GET + JSON.parse，缺失或损坏一律返回 null（fail-soft，宁可重开窗口也不崩）。
 */

import { type WindowRecord } from './decision';
import { type PassiveWindowStore } from './manager';

/** 只用到 get / set 两个方法，便于注入真实 ioredis 或测试假实现。 */
export interface MinimalRedis {
    get(key: string): Promise<string | null>;
    set(key: string, value: string, ...args: (string | number)[]): Promise<'OK' | null>;
}

export class RedisPassiveWindowStore implements PassiveWindowStore {
    constructor(private readonly redis: MinimalRedis) {}

    async markIdempotent(key: string, ttlMs: number): Promise<boolean> {
        const res = await this.redis.set(key, '1', 'PX', ttlMs, 'NX');
        return res === 'OK';
    }

    async getWindow(key: string): Promise<WindowRecord | null> {
        const raw = await this.redis.get(key);
        if (raw === null) return null;
        try {
            const parsed = JSON.parse(raw) as Partial<WindowRecord>;
            if (typeof parsed.windowStart !== 'number' || typeof parsed.replies !== 'number') return null;
            return { windowStart: parsed.windowStart, replies: parsed.replies };
        } catch {
            return null;
        }
    }

    async setWindow(key: string, rec: WindowRecord, ttlMs: number): Promise<void> {
        await this.redis.set(key, JSON.stringify(rec), 'PX', ttlMs);
    }
}
