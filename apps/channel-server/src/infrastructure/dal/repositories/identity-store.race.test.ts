import { describe, it, expect, mock, beforeEach } from 'bun:test';

// 钉死 TypeOrmIdentityStore.upsertMapping 的 race-safe 语义 + 实现禁令：
// 必须用 TypeORM Repository 标准 ORM 接口（findOne / save），禁止裸 SQL
// (AppDataSource.query 调用) 出现在该文件。
//
// 真实 prod bug（已在丢消息）：
//   旧实现用手写 SQL "INSERT ... ON CONFLICT DO NOTHING + UNION ALL SELECT"
//   并发 race 时 UNION ALL 看不到 conflicting row，返回 0 行 fail-loud。
//   之后改成 raw SQL "ON CONFLICT DO UPDATE RETURNING" 修了 race，但 raw SQL
//   是 critical concurrency primitive 上的硬骨头，不该在业务层手写。
//   本次彻底改用 ORM repository.findOne + repository.save（unique violation
//   → catch + 重读）—— PG 内部 visibility 语义交给 TypeORM 处理。
//
// 不连真实 PG —— mock @ormconfig 让我们能验证 ORM 接口的调用形态 + 模拟
// 并发场景下 forward-key 已被别人占用的情况。

interface FakeRow {
    channel: string;
    [key: string]: string;
}

// 模拟 TypeORM Repository：保留 (channel, channelIdCol) 复合唯一约束 +
// internal_id 主键唯一约束的行为。
class FakeRepo {
    rows: FakeRow[] = [];
    findOneCalls = 0;
    saveCalls = 0;
    upsertCalls = 0;
    queryCalls = 0;

    constructor(
        private readonly internalCol: string,
        private readonly channelIdCol: string,
        private readonly forwardConstraint: string,
    ) {}

    async findOne(opts: { where: Record<string, string> }): Promise<FakeRow | null> {
        this.findOneCalls += 1;
        const where = opts.where;
        const hit = this.rows.find((r) =>
            Object.keys(where).every((k) => r[k] === where[k]),
        );
        return hit ?? null;
    }

    async save(row: FakeRow): Promise<FakeRow> {
        this.saveCalls += 1;
        // 模拟 PG forward-key 唯一约束：(channel, channelIdCol) 已存在 → 抛 23505
        const fwdHit = this.rows.find(
            (r) =>
                r.channel === row.channel &&
                r[this.channelIdCol] === row[this.channelIdCol],
        );
        if (fwdHit) {
            const e: Error & { code?: string; constraint?: string } = new Error(
                'duplicate key value violates unique constraint',
            );
            e.code = '23505';
            e.constraint = this.forwardConstraint;
            (e as unknown as { driverError: { code: string; constraint: string } }).driverError = {
                code: '23505',
                constraint: this.forwardConstraint,
            };
            throw e;
        }
        // 模拟 PG 主键唯一约束：internal_id 已存在 → 抛 23505 但 constraint 是主键
        const pkHit = this.rows.find(
            (r) => r[this.internalCol] === row[this.internalCol],
        );
        if (pkHit) {
            const e: Error & { code?: string; constraint?: string } = new Error(
                'duplicate key value violates unique constraint',
            );
            e.code = '23505';
            e.constraint = `identity_pkey`;
            (e as unknown as { driverError: { code: string; constraint: string } }).driverError = {
                code: '23505',
                constraint: `identity_pkey`,
            };
            throw e;
        }
        this.rows.push({ ...row });
        return row;
    }
}

// 注入这些 fake repo 给 TypeOrmIdentityStore 通过 AppDataSource.getRepository
const repos = {
    user: new FakeRepo('internal_user_id', 'channel_user_id', 'uq_identity_user_channel'),
    chat: new FakeRepo('internal_chat_id', 'channel_chat_id', 'uq_identity_chat_channel'),
    message: new FakeRepo('internal_message_id', 'channel_message_id', 'uq_identity_message_channel'),
};

// query() 调用计数 —— 这是禁令：实现里不应该再调用 AppDataSource.query()
let rawQueryCalls = 0;

const getRepositoryMock = mock((entity: { name?: string }) => {
    const name = entity?.name ?? '';
    if (name.includes('User')) return repos.user;
    if (name.includes('Chat')) return repos.chat;
    if (name.includes('Message')) return repos.message;
    throw new Error(`unknown entity: ${name}`);
});

const queryMock = mock(async (_sql: string, _params?: unknown[]) => {
    rawQueryCalls += 1;
    return [];
});

mock.module('@ormconfig', () => ({
    default: {
        getRepository: getRepositoryMock,
        query: queryMock,
    },
}));

mock.module('@entities/identity-mapping', () => ({
    IdentityUser: class { static name = 'IdentityUser'; },
    IdentityChat: class { static name = 'IdentityChat'; },
    IdentityMessage: class { static name = 'IdentityMessage'; },
}));

const { TypeOrmIdentityStore } = await import('./identity-store');
const { PrimaryKeyConflictError } = await import('@core/channels/db-identity-resolver');

describe('TypeOrmIdentityStore.upsertMapping race-safe via TypeORM ORM 接口', () => {
    beforeEach(() => {
        repos.user.rows = [];
        repos.chat.rows = [];
        repos.message.rows = [];
        repos.user.findOneCalls = 0;
        repos.user.saveCalls = 0;
        repos.chat.findOneCalls = 0;
        repos.chat.saveCalls = 0;
        repos.message.findOneCalls = 0;
        repos.message.saveCalls = 0;
        rawQueryCalls = 0;
    });

    it('禁用 raw SQL：上层 ORM 接口收敛后，AppDataSource.query 不应被调用', async () => {
        const store = new TypeOrmIdentityStore();
        await store.upsertMapping({
            kind: 'message',
            channel: 'lark',
            channelId: 'om_first',
            internalId: 'ULID_FRESH________________',
        });
        // 实现必须只用 ORM repository.findOne / repository.save / repository.upsert
        // 等标准接口；query() 不应被调用。
        expect(rawQueryCalls).toBe(0);
    });

    it('forward-key 不存在 -> save 成功返回 internalId', async () => {
        const store = new TypeOrmIdentityStore();
        const id = await store.upsertMapping({
            kind: 'user',
            channel: 'lark',
            channelId: 'uid_1',
            internalId: 'ULID_FRESH_USER___________',
        });
        expect(id).toBe('ULID_FRESH_USER___________');
        expect(repos.user.rows).toHaveLength(1);
        expect(repos.user.rows[0]!.channel_user_id).toBe('uid_1');
        expect(repos.user.rows[0]!.internal_user_id).toBe(
            'ULID_FRESH_USER___________',
        );
    });

    it('forward-key 已存在（race 复现）：upsertMapping 收敛回已有 internalId、不抛错', async () => {
        // 预置已有行（并发竞争场景：别人已经写过同 (channel, channelId)）
        repos.message.rows.push({
            channel: 'lark',
            channel_message_id: 'om_exist',
            internal_message_id: 'EXISTING_ULID_______________',
        });

        const store = new TypeOrmIdentityStore();
        const id = await store.upsertMapping({
            kind: 'message',
            channel: 'lark',
            channelId: 'om_exist',
            internalId: 'CANDIDATE_ULID_NOT_USED____',
        });
        // 必须返回已有的 internalId（不是 candidate）—— ON CONFLICT 收敛语义
        expect(id).toBe('EXISTING_ULID_______________');
        // 仍然只有一行（candidate 没有被新插入）
        expect(repos.message.rows).toHaveLength(1);
    });

    it('forward-key save race（同时写）：第一次 race 抛 23505 forward 约束后，重新读到现有行并返回其 internalId', async () => {
        // 模拟"我先 findOne 看没人，然后 save 时 race 输了"的并发：findOne
        // 阶段为空，但 save 时另一个并发已写入。实现应捕获 23505 + 重读，
        // 拿到现有行的 internalId 返回。
        const store = new TypeOrmIdentityStore();

        // 在 save 之前篡改 rows：第一次 findOne 返回 null，save 触发 23505，
        // 实现再重新 findOne 拿到这条 race-winner 的 internalId
        let saveAttempt = 0;
        const origSave = repos.chat.save.bind(repos.chat);
        repos.chat.save = (async (row: FakeRow) => {
            saveAttempt += 1;
            if (saveAttempt === 1) {
                // 模拟 race winner 抢先写入
                repos.chat.rows.push({
                    channel: row.channel,
                    channel_chat_id: row.channel_chat_id!,
                    internal_chat_id: 'WINNER_ULID________________',
                });
            }
            return origSave(row);
        }) as typeof repos.chat.save;

        const id = await store.upsertMapping({
            kind: 'chat',
            channel: 'lark',
            channelId: 'oc_race',
            internalId: 'LOSER_CANDIDATE_ULID_______',
        });
        // 实现必须返回 race winner 的 internalId（不是 candidate 也不抛错）
        expect(id).toBe('WINNER_ULID________________');
    });

    it('internal_id 主键(ULID)冲突：抛 PrimaryKeyConflictError 让 resolver 重新生成', async () => {
        // 预置一行占用某个 ULID（不同 forward-key），下次 upsert 用同一 ULID
        // 但新 forward-key —— 必须抛 PrimaryKeyConflictError，不能静默收敛
        repos.user.rows.push({
            channel: 'qq',
            channel_user_id: 'qq_uid_1',
            internal_user_id: 'TAKEN_ULID________________',
        });

        const store = new TypeOrmIdentityStore();
        await expect(
            store.upsertMapping({
                kind: 'user',
                channel: 'lark',
                channelId: 'different_uid',
                internalId: 'TAKEN_ULID________________',
            }),
        ).rejects.toBeInstanceOf(PrimaryKeyConflictError);
    });
});
