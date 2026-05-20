import { describe, it, expect } from 'bun:test';

import { runIdentityResolverContract } from './identity-resolver-contract';
import {
    DbIdentityResolver,
    PrimaryKeyConflictError,
    type IdentityStore,
    type IdentityRow,
} from './db-identity-resolver';
import type { IdentityKind } from './identity-resolver';

// DbIdentityResolver 的单测用一个内存版 IdentityStore 顶替真实 DB（生产走
// TypeORM 实现，运行时才接真实 PG）。这个 fake 必须忠实模拟真实表 upsert 的
// 关键语义：upsertMapping 走 INSERT ... ON CONFLICT (channel, channel_*_id)
// DO NOTHING 然后回取 internal_id——并发首次出现下永远收敛到同一个
// internal_id，且不依赖外层事务是否存在/隔离级别。PK(ULID) 冲突单独信号。

interface Stored extends IdentityRow {}

// 行为可控的内存 store，模拟真实 PG 的两类约束：
//   - forward-key (kind, channel, channelId) 复合唯一：ON CONFLICT DO NOTHING
//     语义——已存在则不插，直接收敛回已有 internalId（绝不抛错）。
//   - internal_*_id 主键(ULID)唯一：极罕见撞了抛 PrimaryKeyConflictError，
//     resolver 负责重新生成 ULID 重试，不当 forward-key 冲突。
class FakeIdentityStore implements IdentityStore {
    private rows: Stored[] = [];
    upsertCalls = 0;
    // 注入一组"伪造的 ULID"，让前 N 次 upsert 的 internalId 撞已占用主键，
    // 用来确定性地触发 PK 冲突重试路径（真实 PG 下概率极低、无法稳定复现）。
    poisonedInternalIds = new Set<string>();

    seedOccupiedInternalId(kind: IdentityKind, internalId: string): void {
        // 占位一行（不同 forward-key），让后续不同来源若复用同一 ULID 撞主键
        this.rows.push({
            kind,
            channel: '__seed__',
            channelId: `__seed__${internalId}`,
            internalId,
        });
    }

    async findInternalId(
        kind: IdentityKind,
        channel: string,
        channelId: string,
    ): Promise<string | null> {
        const hit = this.rows.find(
            (r) =>
                r.kind === kind &&
                r.channel === channel &&
                r.channelId === channelId,
        );
        return hit ? hit.internalId : null;
    }

    async findChannelRef(
        kind: IdentityKind,
        internalId: string,
    ): Promise<{ channel: string; channelId: string } | null> {
        const hit = this.rows.find(
            (r) => r.kind === kind && r.internalId === internalId,
        );
        return hit ? { channel: hit.channel, channelId: hit.channelId } : null;
    }

    async upsertMapping(row: IdentityRow): Promise<string> {
        this.upsertCalls += 1;
        // forward-key 已存在：ON CONFLICT DO NOTHING -> 回取已有 internalId，
        // 永远收敛到同一个，绝不抛错（这是不依赖外层事务的关键）。
        const fwd = this.rows.find(
            (r) =>
                r.kind === row.kind &&
                r.channel === row.channel &&
                r.channelId === row.channelId,
        );
        if (fwd) return fwd.internalId;

        // forward-key 不存在但拿到的 internalId 撞了主键(ULID)：单独信号，
        // 让 resolver 重新生成 ULID 重试，绝不当 forward-key 冲突收敛。
        const pkClash = this.rows.find(
            (r) => r.kind === row.kind && r.internalId === row.internalId,
        );
        if (pkClash) {
            throw new PrimaryKeyConflictError(
                row.kind,
                row.internalId,
            );
        }
        this.rows.push({ ...row });
        return row.internalId;
    }
}

// 跑共享契约（含并发不产生重复全局 ID 一条）。
runIdentityResolverContract('Db+FakeStore', () => {
    return new DbIdentityResolver(new FakeIdentityStore());
});

describe('DbIdentityResolver upsert 写路径与冲突处理', () => {
    it('并发 resolve 同 (kind,channel,channelId) 经 upsert 收敛到单一 internal id', async () => {
        const store = new FakeIdentityStore();
        const r = new DbIdentityResolver(store);

        const ids = await Promise.all(
            Array.from({ length: 8 }, () => r.resolve('user', 'qq', 'dup')),
        );
        expect(new Set(ids).size).toBe(1);
        // 每个并发都进了 upsert（不再 check-then-insert），最终单一全局 ID
        expect(store.upsertCalls).toBe(8);
        expect(await r.toChannel('user', ids[0]!)).toEqual({
            channel: 'qq',
            channelId: 'dup',
        });
    });

    it('resolve 包在模拟外层事务（一旦写冲突就毒化后续读）下仍收敛单一 internal_id', async () => {
        // 模拟"接线后 resolve 被包进外层显式 PG 事务"的脆弱点：旧 check→insert→
        // catch 23505→回读 会让事务进 aborted、回读全失败。upsert 化后 resolver
        // 不再依赖 catch+回读，单 SQL 收敛，外层事务即使因别的写已脆弱也不影响。
        const store = new FakeIdentityStore();
        // 包一层：findInternalId 在"事务已脆弱"时一律抛错，模拟 aborted txn
        // 下回读失效。若 resolver 还依赖 check/回读，这里必崩。
        let txnPoisoned = false;
        const txnStore: IdentityStore = {
            findInternalId: async (k, c, id) => {
                if (txnPoisoned) {
                    throw new Error('current transaction is aborted');
                }
                return store.findInternalId(k, c, id);
            },
            findChannelRef: (k, id) => store.findChannelRef(k, id),
            upsertMapping: async (row) => {
                // 第一次写后标记事务脆弱：之后任何 check-then-insert 回读都崩
                const out = await store.upsertMapping(row);
                txnPoisoned = true;
                return out;
            },
        };
        const r = new DbIdentityResolver(txnStore);
        const a = await r.resolve('chat', 'qq', 'txn-1');
        // 第二次：事务已脆弱，若 resolver 还走 check/回读会抛 aborted；
        // upsert 化后只调 upsertMapping，DO NOTHING 收敛回 a，不读。
        const b = await r.resolve('chat', 'qq', 'txn-1');
        expect(a).toBe(b);
    });

    it('ULID 主键冲突触发重新生成有限次重试，不误当 forward-key 冲突抛错', async () => {
        const store = new FakeIdentityStore();
        const r = new DbIdentityResolver(store);

        // 先正常分配一个全局 ID，拿到它真实占用的 ULID
        const taken = await r.resolve('user', 'lark', 'first');
        // 占位让"下一次新来源若复用同一 ULID"撞主键。通过覆盖 generateUlid
        // 注入：让 resolver 第一次生成的 ULID 恰好等于 taken（撞主键），
        // 第二次生成一个新 ULID（成功）。
        const seq = [taken, 'ZZZZZZZZZZZZZZZZZZZZZZZZZZ'];
        let i = 0;
        const r2 = new DbIdentityResolver(store, () => seq[i++]!);
        // 新来源 (lark, second)：forward-key 不存在，第一次 ULID=taken 撞主键，
        // 必须重生成（第二个 ULID）成功，绝不当 forward-key 冲突收敛回 taken。
        const id = await r2.resolve('user', 'lark', 'second');
        expect(id).toBe('ZZZZZZZZZZZZZZZZZZZZZZZZZZ');
        expect(id).not.toBe(taken);
        expect(await r2.toChannel('user', id)).toEqual({
            channel: 'lark',
            channelId: 'second',
        });
    });

    it('ULID 主键冲突重试超过上限仍冲突时抛错，不死循环', async () => {
        const store = new FakeIdentityStore();
        // 预占一个 ULID，并让生成器永远吐这个被占的 ULID
        await new DbIdentityResolver(store, () => 'AAAAAAAAAAAAAAAAAAAAAAAAAA').resolve(
            'user',
            'lark',
            'occupier',
        );
        const rBad = new DbIdentityResolver(
            store,
            () => 'AAAAAAAAAAAAAAAAAAAAAAAAAA',
        );
        await expect(
            rBad.resolve('user', 'lark', 'victim'),
        ).rejects.toThrow();
    });

    it('生成的 internal id 形如 ULID（26 位 Crockford base32，大写）', async () => {
        const r = new DbIdentityResolver(new FakeIdentityStore());
        const id = await r.resolve('message', 'lark', 'om_x');
        expect(id).toMatch(/^[0-9A-HJKMNP-TV-Z]{26}$/);
    });
});
