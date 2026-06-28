import { describe, it, expect } from 'bun:test';
import { RedisPassiveWindowStore, type MinimalRedis } from './redis-store';

/** 记录调用参数的假 redis，模拟 NX 语义。 */
class FakeRedis implements MinimalRedis {
    store = new Map<string, string>();
    setCalls: unknown[][] = [];

    async get(key: string): Promise<string | null> {
        return this.store.get(key) ?? null;
    }

    async set(key: string, value: string, ...args: (string | number)[]): Promise<'OK' | null> {
        this.setCalls.push([key, value, ...args]);
        const nx = args.includes('NX');
        if (nx && this.store.has(key)) return null;
        this.store.set(key, value);
        return 'OK';
    }
}

describe('RedisPassiveWindowStore: markIdempotent', () => {
    it('uses SET key 1 PX <ttl> NX and returns true on first write, false when present', async () => {
        const redis = new FakeRedis();
        const store = new RedisPassiveWindowStore(redis);

        const first = await store.markIdempotent('k', 1000);
        expect(first).toBe(true);
        const second = await store.markIdempotent('k', 1000);
        expect(second).toBe(false);

        // verify the ioredis argument order: set('k', '1', 'PX', 1000, 'NX')
        expect(redis.setCalls[0]).toEqual(['k', '1', 'PX', 1000, 'NX']);
    });
});

describe('RedisPassiveWindowStore: window record round-trip', () => {
    it('JSON-encodes on setWindow (with PX ttl) and decodes on getWindow', async () => {
        const redis = new FakeRedis();
        const store = new RedisPassiveWindowStore(redis);

        await store.setWindow('w', { windowStart: 123, replies: 2 }, 5000);
        expect(redis.setCalls[0]).toEqual(['w', JSON.stringify({ windowStart: 123, replies: 2 }), 'PX', 5000]);

        const rec = await store.getWindow('w');
        expect(rec).toEqual({ windowStart: 123, replies: 2 });
    });

    it('returns null for a missing key', async () => {
        const store = new RedisPassiveWindowStore(new FakeRedis());
        expect(await store.getWindow('nope')).toBeNull();
    });

    it('returns null (does not throw) on malformed stored JSON', async () => {
        const redis = new FakeRedis();
        redis.store.set('bad', '{not json');
        const store = new RedisPassiveWindowStore(redis);
        expect(await store.getWindow('bad')).toBeNull();
    });
});
