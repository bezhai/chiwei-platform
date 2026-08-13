// 幂等占位协议的单测。跑的是**真的 store**，只把 redis 换成一份带值的假的 —— 串行的
// 假 store 测不出这里唯一要紧的两件事：原子性，和两个服务对同一个值的解读。

import { describe, expect, it } from 'bun:test';

import {
    CLAIM_DONE,
    CLAIM_DONE_TTL_SECONDS,
    CLAIM_IN_FLIGHT,
    CLAIM_LEASE_SECONDS,
    createLaneClaimStore,
    type LaneRedis,
} from './lane-claim';

const KEY = 'inbound_lane:lark:im.message.receive_v1:cm_1:ppe-x';

/**
 * 假 redis。NX 语义照真的来（已存在就不写、返回 null），每条命令开头让出一次微任务，
 * 好让并发的调用真的交错 —— 判断和写入之间不许有 await，否则这个假的比真的松，
 * 测不出并发。
 */
function fakeRedis(initial: Record<string, string> = {}) {
    const keys = new Map<string, { value: string; ttl?: number }>(
        Object.entries(initial).map(([k, v]) => [k, { value: v }]),
    );
    const writes: string[] = [];

    const redis: LaneRedis = {
        setNx: async (key, value, seconds) => {
            await Promise.resolve();
            if (keys.has(key)) return null;
            writes.push(`setNx ${key}=${value} ttl=${seconds}`);
            keys.set(key, { value, ttl: seconds });
            return 'OK';
        },
        get: async (key) => {
            await Promise.resolve();
            return keys.get(key)?.value ?? null;
        },
        setWithExpire: async (key, value, seconds) => {
            await Promise.resolve();
            writes.push(`setWithExpire ${key}=${value} ttl=${seconds}`);
            keys.set(key, { value, ttl: seconds });
            return 'OK';
        },
        del: async (...del) => {
            await Promise.resolve();
            let removed = 0;
            for (const key of del) {
                writes.push(`del ${key}`);
                if (keys.delete(key)) removed += 1;
            }
            return removed;
        },
    };

    return { redis, keys, writes };
}

// ⚠️ 跨服务契约。channel-server 读写同一批 key（它那侧的 inbound-lane-claim.ts 钉了
// 逐字相同的两个字面量）。两个 app 是两个包，编译期对不上，只能两边各钉一条。
//
// 值对不上的后果不对称，两个方向各错各的：
//   完成标记写成别的（比如 '1'）→ 对面判成"有人正在处理"，一直退回队列，直到 24h TTL
//                                 过期后**重新处理一遍**
//   占位标记被对面当成"已处理"  → 消息被直接 ACK 掉，**真丢**
describe('claim 协议的线上值', () => {
    it('marks an in-flight claim with the value channel-server also writes', () => {
        expect(CLAIM_IN_FLIGHT).toBe('in-flight');
    });

    it('marks a finished message with the value channel-server also writes', () => {
        expect(CLAIM_DONE).toBe('done');
    });

    // 租约短，因为它回答的是"持有者崩了之后多久能被别人重新处理"。完成标记要盖住 MQ
    // 的重投窗口，所以长得多。
    it('leases a claim for far less time than it keeps a completion', () => {
        expect(CLAIM_LEASE_SECONDS).toBe(5 * 60);
        expect(CLAIM_DONE_TTL_SECONDS).toBe(24 * 60 * 60);
    });
});

describe('createLaneClaimStore', () => {
    it('claims a key nobody holds, with a lease on it', async () => {
        const redis = fakeRedis();
        const store = createLaneClaimStore(() => redis.redis);

        expect(await store.claim(KEY)).toBe('claimed');
        expect(redis.keys.get(KEY)).toEqual({ value: CLAIM_IN_FLIGHT, ttl: CLAIM_LEASE_SECONDS });
    });

    // 先查再写是两步，两个 Pod 能同时穿过查询、各执行一遍副作用（各回一条消息）。
    // 这条测试就是问"两个人同时伸手，是不是只有一个拿到"。
    it('lets exactly one of two simultaneous consumers win', async () => {
        const redis = fakeRedis();
        const store = createLaneClaimStore(() => redis.redis);

        const [a, b] = await Promise.all([store.claim(KEY), store.claim(KEY)]);

        expect([a, b].sort()).toEqual(['claimed', 'in-flight']);
    });

    it('reads a finished message as done', async () => {
        const redis = fakeRedis({ [KEY]: CLAIM_DONE });
        const store = createLaneClaimStore(() => redis.redis);

        expect(await store.claim(KEY)).toBe('done');
    });

    // 没占到 ≠ 已处理。分不清的话，对方还没写完成标记就把消息 ACK 掉了。
    it('never mistakes someone else holding it for the work being finished', async () => {
        const redis = fakeRedis({ [KEY]: CLAIM_IN_FLIGHT });
        const store = createLaneClaimStore(() => redis.redis);

        expect(await store.claim(KEY)).toBe('in-flight');
    });

    // 分成 SET 再 EXPIRE 两步的话，中间崩掉会留下一个永不过期的 key。
    it('writes the completion in a single command, expiry and all', async () => {
        const redis = fakeRedis();
        const store = createLaneClaimStore(() => redis.redis);

        await store.claim(KEY);
        redis.writes.length = 0;
        await store.complete(KEY);

        expect(redis.writes).toEqual([
            `setWithExpire ${KEY}=${CLAIM_DONE} ttl=${CLAIM_DONE_TTL_SECONDS}`,
        ]);
        expect(redis.keys.get(KEY)).toEqual({ value: CLAIM_DONE, ttl: CLAIM_DONE_TTL_SECONDS });
    });

    // 处理失败要立刻把占位还回去，否则重投的那一条会看到"有人在处理"，白等一个租约。
    it('frees the key on release so a retry can claim it again', async () => {
        const redis = fakeRedis();
        const store = createLaneClaimStore(() => redis.redis);

        await store.claim(KEY);
        await store.release(KEY);

        expect(redis.keys.has(KEY)).toBe(false);
        expect(await store.claim(KEY)).toBe('claimed');
    });
});
