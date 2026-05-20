// IdentityResolver 的 DB 持久化实现（T5）。读写三张映射表 identity_user /
// identity_chat / identity_message，满足 T1 identity-resolver.ts 钉死的同一套契约。
//
// 与 ORM 解耦：本模块只依赖结构型接口 IdentityStore（参考 lark-credentials.ts
// 的 ChannelCredentialed 做法），不 import 任何 TypeORM 实体或数据源，单测可纯跑。
// 生产运行时由 infrastructure 层提供一个 TypeORM 实现注入进来。
//
// 三类 kind（user/chat/message）是三个独立命名空间——store 用 kind 区分到底
// 落哪张表，三张表结构相同。
//
// 写路径用单 SQL upsert（INSERT ... ON CONFLICT (channel, channel_*_id)
// DO UPDATE ... RETURNING），不再 check-then-insert-catch：
//   - 不依赖 TypeORM 默认 autocommit / READ COMMITTED——即使后续接线把 resolve
//     包进外层显式 PG 事务，也不会因 23505 让事务进 aborted 后回读全失败；
//   - forward-key (channel, channel_*_id) 冲突由 ON CONFLICT DO UPDATE 在
//     引擎内收敛，DO UPDATE 永远 RETURNING 那条行（哪怕 SET 是 no-op），
//     永远拿回同一个 internal_id（旧实现用 DO NOTHING + CTE UNION ALL
//     SELECT 兜底，read committed 下并发同 forward-key 会让 UNION ALL 的
//     SELECT 看不到 conflicting row，返回 0 行让 fail-loud 误伤）；
//   - internal_*_id 主键(ULID)冲突是另一类（store 抛 PrimaryKeyConflictError），
//     极罕见，由本模块重新生成 ULID 有限次重试，绝不当 forward-key 冲突收敛。

import {
    type IdentityResolver,
    type IdentityKind,
    type ChannelRef,
    IdentityNotFoundError,
} from './identity-resolver';

// 一条身份映射行（kind 决定它属于三张表里的哪一张）。
export interface IdentityRow {
    kind: IdentityKind;
    channel: string;
    channelId: string;
    internalId: string;
}

// internal_*_id 主键(ULID)唯一约束被违反时抛这个。与 forward-key 冲突严格区分：
// forward-key 冲突由 upsert 的 ON CONFLICT DO UPDATE 在 DB 引擎内静默收敛
// （store 不抛错、DO UPDATE RETURNING 直接拉回已有 internalId）；只有 ULID 主键撞了（同一 ULID 被
// 两条不同 forward-key 占用，128bit 熵下概率极低）才抛本错误，让 resolver
// 重新生成 ULID 重试，绝不把它误当 forward-key 冲突收敛到别人的映射。
export class PrimaryKeyConflictError extends Error {
    constructor(
        public readonly kind: IdentityKind,
        public readonly internalId: string,
    ) {
        super(
            `identity ${kind} primary key (ULID) conflict for internal id "${internalId}"`,
        );
        this.name = 'PrimaryKeyConflictError';
    }
}

// DbIdentityResolver 对底层存储的全部需求。结构型接口，不绑 ORM。
export interface IdentityStore {
    // (kind, channel, channelId) -> internalId，没有返回 null。
    findInternalId(
        kind: IdentityKind,
        channel: string,
        channelId: string,
    ): Promise<string | null>;

    // (kind, internalId) -> (channel, channelId)，没有返回 null。
    findChannelRef(
        kind: IdentityKind,
        internalId: string,
    ): Promise<{ channel: string; channelId: string } | null>;

    // upsert 语义（单 SQL，事务安全无脆弱假设）：
    //   INSERT ... ON CONFLICT (channel, channel_*_id) DO UPDATE
    //     SET <channel_id_col> = EXCLUDED.<channel_id_col>
    //   RETURNING <internal_id_col>
    //   —— DO UPDATE 永远 RETURNING 那条行（哪怕 SET 是 no-op），不再需要
    //   CTE UNION ALL SELECT 兜底；旧 DO NOTHING + UNION ALL 在 PG read
    //   committed 下并发同 forward-key 会让 UNION ALL 的 SELECT 看不到
    //   conflicting row 返回 0 行 → fail-loud 丢消息。
    // 行为契约：
    //   - forward-key (kind, channel, channelId) 已存在 -> 不插入，返回已有
    //     internalId（并发首次出现时所有竞争者都收敛到同一个；绝不抛错，
    //     不依赖外层事务是否存在/隔离级别）。
    //   - forward-key 不存在 -> 插入 row 并返回 row.internalId。
    //   - 插入时 internal_*_id 主键(ULID)撞了 -> 抛 PrimaryKeyConflictError
    //     （区别于 forward-key 冲突；由 resolver 重新生成 ULID 重试）。
    upsertMapping(row: IdentityRow): Promise<string>;
}

// ULID 生成。spec 要求"用 ULID 这类，保证不同 channel 不会撞"。
// 选 ULID 而不是 UUIDv4：ULID 按时间前缀单调递增、做主键时索引/分区比随机
// UUID 友好，且 128bit 随机熵足够全局唯一、跨 channel 不会撞；T1 InMemory 用
// randomUUID 只是占位实现，契约只要求"全局唯一字符串"，DB 版按 spec 选 ULID。
const CROCKFORD = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

function generateUlid(now: number = Date.now()): string {
    // 48bit 时间戳（毫秒）-> 10 个 Crockford base32 字符
    let ts = now;
    const time: string[] = new Array(10);
    for (let i = 9; i >= 0; i--) {
        time[i] = CROCKFORD[ts % 32]!;
        ts = Math.floor(ts / 32);
    }
    // 80bit 随机 -> 16 个字符
    const rnd = new Uint8Array(10);
    crypto.getRandomValues(rnd);
    let bits = 0;
    let value = 0;
    const rand: string[] = [];
    for (let i = 0; i < rnd.length; i++) {
        value = (value << 8) | rnd[i]!;
        bits += 8;
        while (bits >= 5) {
            bits -= 5;
            rand.push(CROCKFORD[(value >>> bits) & 31]!);
        }
    }
    if (bits > 0) {
        rand.push(CROCKFORD[(value << (5 - bits)) & 31]!);
    }
    return time.join('') + rand.slice(0, 16).join('');
}

// ULID 主键冲突重试上限。128bit 熵下单次撞已极罕见，连撞多次实质不可能；
// 若真的连撞这么多次说明 store / 生成器异常，明确抛错而不是无限重试。
const MAX_PK_RETRIES = 5;

export class DbIdentityResolver implements IdentityResolver {
    // genUlid 可注入：生产用真实 ULID 生成器；单测注入确定性序列以稳定复现
    // PK 冲突重试路径（真实 PG 下该路径概率极低、无法稳定触发）。
    constructor(
        private readonly store: IdentityStore,
        private readonly genUlid: () => string = generateUlid,
    ) {}

    async resolve(
        kind: IdentityKind,
        channel: string,
        channelId: string,
    ): Promise<string> {
        // 先读：命中即幂等返回（绝大多数请求走这条，不写）。这一步纯属
        // 快路径优化——正确性完全由下面的 upsert 兜底（ON CONFLICT 在 DB
        // 引擎内收敛）。因此读失败不致命：接线后若 resolve 被包进外层显式
        // 事务且该事务已因别的写 aborted，回读会抛 "transaction is aborted"，
        // 此时不该让 resolve 跟着崩，而是落到事务安全的 upsert 路径上。
        try {
            const existing = await this.store.findInternalId(
                kind,
                channel,
                channelId,
            );
            if (existing !== null) return existing;
        } catch {
            // 快路径读失败 -> 忽略，直接走 upsert（不依赖外层事务/隔离级别）。
        }

        // 未命中：走单 SQL upsert（ON CONFLICT (channel, channel_*_id)
        // DO UPDATE ... RETURNING）。forward-key 冲突由 DB 引擎在单条语句
        // 内收敛——无 check-then-insert 竞态，不依赖外层事务/隔离级别。
        // 唯一需要应用层处理的是 internal_*_id 主键(ULID)冲突：换 ULID 重试。
        let lastErr: unknown;
        for (let attempt = 0; attempt < MAX_PK_RETRIES; attempt++) {
            const internalId = this.genUlid();
            try {
                return await this.store.upsertMapping({
                    kind,
                    channel,
                    channelId,
                    internalId,
                });
            } catch (err) {
                if (!(err instanceof PrimaryKeyConflictError)) throw err;
                // ULID 撞主键（极罕见）：重新生成下一个 ULID 再试，
                // 绝不当 forward-key 冲突收敛到别人的映射。
                lastErr = err;
            }
        }
        throw new Error(
            `identity ${kind} (${channel}, ${channelId}): ULID primary key ` +
                `conflict persisted after ${MAX_PK_RETRIES} retries; ` +
                `store or ULID generator is inconsistent`,
            { cause: lastErr },
        );
    }

    async toChannel(
        kind: IdentityKind,
        internalId: string,
    ): Promise<ChannelRef> {
        const ref = await this.store.findChannelRef(kind, internalId);
        if (ref === null) throw new IdentityNotFoundError(kind, internalId);
        return { channel: ref.channel, channelId: ref.channelId };
    }
}
