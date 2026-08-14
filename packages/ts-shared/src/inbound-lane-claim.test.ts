// 泳道信封幂等占位协议的单测。
//
// 这里钉的每一条都是**跨服务契约**：channel-server 和 lark-service 读写 redis 里同一批
// key，两边算出的 key、写下的值、给的 TTL 差一个字节都不再是同一把锁 —— 同一条信封会被
// 两个进程各认领成功一次，用户看到两条回复。所以 key 的格式和协议的值都用字面量钉死，
// 改动必须是一次自觉的行为，而不是重构顺手带出来的。
//
// 跑的是**真的 store**，只把 redis 换成一份带值的假的：串行的假 store 测不出这里唯一要
// 紧的两件事 —— 原子性，和两个服务对同一个值的解读。

import { describe, expect, it } from 'bun:test';

import {
    CLAIM_DONE,
    CLAIM_DONE_TTL_SECONDS,
    CLAIM_IN_FLIGHT,
    CLAIM_LEASE_SECONDS,
    createInboundLaneClaims,
    inboundLaneClaimKey,
    type ClaimRedis,
} from './inbound-lane-claim';

const PARTS = {
    channel: 'lark',
    eventType: 'im.message.receive_v1',
    globalMessageId: 'cm_1',
    lane: 'ppe-x',
};

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

    const redis: ClaimRedis = {
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

// key 是两个服务认出"这条我处理过了"的唯一依据。字面量钉死：格式变了就是换了一把锁，
// 而滚动发布期间新旧 Pod 同时在跑，两把锁等于没锁。
describe('inboundLaneClaimKey', () => {
    it('spells out one processing of one event, on one channel and one lane', () => {
        expect(inboundLaneClaimKey(PARTS)).toBe(KEY);
    });

    // key 里没有队列名这一段，而且函数根本收不到队列 —— 这是刻意的：分区迁移期间同一条
    // 信封可能从共享队列来、也可能从分区队列来，key 认队列的话两条各算一次。
    it('tells the same message on two channels apart', () => {
        expect(inboundLaneClaimKey({ ...PARTS, channel: 'qq' })).not.toBe(
            inboundLaneClaimKey(PARTS),
        );
    });

    it('tells the same message on two lanes apart', () => {
        expect(inboundLaneClaimKey({ ...PARTS, lane: 'coe-y' })).not.toBe(
            inboundLaneClaimKey(PARTS),
        );
    });

    it('tells two events on the same message apart', () => {
        expect(inboundLaneClaimKey({ ...PARTS, eventType: 'card.action.trigger' })).not.toBe(
            inboundLaneClaimKey(PARTS),
        );
    });

    it('tells two messages apart', () => {
        expect(inboundLaneClaimKey({ ...PARTS, globalMessageId: 'cm_2' })).not.toBe(
            inboundLaneClaimKey(PARTS),
        );
    });
});

// 值对不上的后果不对称，两个方向各错各的：
//   完成标记写成别的（比如 '1'）→ 对面判成"有人正在处理"，一直退回队列，直到 24h TTL
//                                 过期后**重新处理一遍**
//   占位标记被对面当成"已处理"  → 消息被直接 ACK 掉，**真丢**
describe('claim 协议的线上值', () => {
    it('marks an in-flight claim with a value both services read the same way', () => {
        expect(CLAIM_IN_FLIGHT).toBe('in-flight');
    });

    it('marks a finished message with a value both services read the same way', () => {
        expect(CLAIM_DONE).toBe('done');
    });

    // 租约短，因为它回答的是"持有者崩了之后多久能被别人重新处理"。完成标记要盖住 MQ
    // 的重投窗口，所以长得多。
    it('leases a claim for far less time than it keeps a completion', () => {
        expect(CLAIM_LEASE_SECONDS).toBe(5 * 60);
        expect(CLAIM_DONE_TTL_SECONDS).toBe(24 * 60 * 60);
    });
});

describe('createInboundLaneClaims', () => {
    it('claims a key nobody holds, with a lease on it', async () => {
        const redis = fakeRedis();
        const claims = createInboundLaneClaims(() => redis.redis);

        expect(await claims.claim(KEY)).toBe('claimed');
        expect(redis.keys.get(KEY)).toEqual({ value: CLAIM_IN_FLIGHT, ttl: CLAIM_LEASE_SECONDS });
    });

    // 先查再写是两步，两个 Pod 能同时穿过查询、各执行一遍副作用（各回一条消息）。
    // 这条测试就是问"两个人同时伸手，是不是只有一个拿到"。
    it('lets exactly one of two simultaneous consumers win', async () => {
        const redis = fakeRedis();
        const claims = createInboundLaneClaims(() => redis.redis);

        const [a, b] = await Promise.all([claims.claim(KEY), claims.claim(KEY)]);

        expect([a, b].sort()).toEqual(['claimed', 'in-flight']);
    });

    it('reads a finished message as done', async () => {
        const redis = fakeRedis({ [KEY]: CLAIM_DONE });
        const claims = createInboundLaneClaims(() => redis.redis);

        expect(await claims.claim(KEY)).toBe('done');
    });

    // 没占到 ≠ 已处理。分不清的话，对方还没写完成标记就把消息 ACK 掉了。
    it('never mistakes someone else holding it for the work being finished', async () => {
        const redis = fakeRedis({ [KEY]: CLAIM_IN_FLIGHT });
        const claims = createInboundLaneClaims(() => redis.redis);

        expect(await claims.claim(KEY)).toBe('in-flight');
    });

    // 分成 SET 再 EXPIRE 两步的话，中间崩掉会留下一个永不过期的 key。
    it('writes the completion in a single command, expiry and all', async () => {
        const redis = fakeRedis();
        const claims = createInboundLaneClaims(() => redis.redis);

        await claims.claim(KEY);
        redis.writes.length = 0;
        await claims.complete(KEY);

        expect(redis.writes).toEqual([
            `setWithExpire ${KEY}=${CLAIM_DONE} ttl=${CLAIM_DONE_TTL_SECONDS}`,
        ]);
        expect(redis.keys.get(KEY)).toEqual({ value: CLAIM_DONE, ttl: CLAIM_DONE_TTL_SECONDS });
    });

    // 处理失败要立刻把占位还回去，否则重投的那一条会看到"有人在处理"，白等一个租约。
    it('frees the key on release so a retry can claim it again', async () => {
        const redis = fakeRedis();
        const claims = createInboundLaneClaims(() => redis.redis);

        await claims.claim(KEY);
        await claims.release(KEY);

        expect(redis.keys.has(KEY)).toBe(false);
        expect(await claims.claim(KEY)).toBe('claimed');
    });
});
