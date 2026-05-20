// 生产运行时的 IdentityStore 实现：用 TypeORM Repository 标准 ORM 接口
// （findOne / save）读写 identity_user / identity_chat / identity_message
// 三张映射表。
//
// 设计：findOne -> 命中即返回；未命中走 save；save 撞 forward-key 唯一约束
// (23505 on uq_identity_*_channel) → race winner 已抢先写入 → 重新 findOne 拿
// 现有行返回。撞主键 (23505 on identity_*_pkey) → 抛 PrimaryKeyConflictError
// 让 DbIdentityResolver 换 ULID 重试。
//
// 为什么不用 raw SQL ON CONFLICT DO UPDATE RETURNING？之前那版工作，但 raw
// SQL 是 critical concurrency primitive 上的硬骨头：表名/列名/约束名靠字符串
// 拼，迁移容易漏；ON CONFLICT 的 visibility 细节难维护。改用 ORM 标准接口
// 后，PG-internal visibility 语义交给 TypeORM；race 安全靠 try-save +
// catch-unique-violation + 重读这种与 ORM 同构的模式，业务代码不再碰 SQL 字
// 符串。
//
// 单测不 import 本文件实际接 TypeORM 数据源——它通过 mock @ormconfig 的
// getRepository 注入 fake repo 验证调用形态。DbIdentityResolver 的单测走
// 内存版 FakeIdentityStore（不经过本文件）。

import { QueryFailedError, type Repository } from 'typeorm';
import AppDataSource from '@ormconfig';
import {
    IdentityUser,
    IdentityChat,
    IdentityMessage,
} from '@entities/identity-mapping';
import type { IdentityKind } from '@core/channels/identity-resolver';
import {
    type IdentityStore,
    type IdentityRow,
    PrimaryKeyConflictError,
} from '@core/channels/db-identity-resolver';

// 每个 kind 对应哪张实体、哪两列名、forward-key 约束名（与 DDL 一致，用于
// 区分 23505 是 forward-key 冲突还是主键冲突）。三张表结构相同，只是列名/
// 约束名带各自前缀。
const KIND_TABLE = {
    user: {
        entity: IdentityUser,
        internalCol: 'internal_user_id',
        channelIdCol: 'channel_user_id',
        forwardConstraint: 'uq_identity_user_channel',
    },
    chat: {
        entity: IdentityChat,
        internalCol: 'internal_chat_id',
        channelIdCol: 'channel_chat_id',
        forwardConstraint: 'uq_identity_chat_channel',
    },
    message: {
        entity: IdentityMessage,
        internalCol: 'internal_message_id',
        channelIdCol: 'channel_message_id',
        forwardConstraint: 'uq_identity_message_channel',
    },
} as const;

// PG 唯一约束违反 sqlstate。
const PG_UNIQUE_VIOLATION = '23505';

// 把 driverError 拆出来：PG 在 23505 时附带 constraint（违反的约束名），
// 用它区分 forward-key 冲突 vs 主键 (ULID) 冲突。
function pgError(
    err: unknown,
): { code?: string; constraint?: string } | null {
    if (err instanceof QueryFailedError) {
        const d = (
            err as unknown as {
                driverError?: { code?: string; constraint?: string };
            }
        ).driverError;
        return d ?? null;
    }
    // 测试 / 部分 driver 直接把 code/constraint 挂在 Error 上；同样支持。
    if (err && typeof err === 'object') {
        const e = err as { code?: string; constraint?: string; driverError?: { code?: string; constraint?: string } };
        if (e.driverError) return e.driverError;
        if (e.code) return { code: e.code, constraint: e.constraint };
    }
    return null;
}

export class TypeOrmIdentityStore implements IdentityStore {
    private repoFor<K extends IdentityKind>(
        kind: K,
    ): {
        repo: Repository<object>;
        internalCol: string;
        channelIdCol: string;
        forwardConstraint: string;
    } {
        const t = KIND_TABLE[kind];
        return {
            repo: AppDataSource.getRepository(t.entity) as unknown as Repository<object>,
            internalCol: t.internalCol,
            channelIdCol: t.channelIdCol,
            forwardConstraint: t.forwardConstraint,
        };
    }

    async findInternalId(
        kind: IdentityKind,
        channel: string,
        channelId: string,
    ): Promise<string | null> {
        const { repo, internalCol, channelIdCol } = this.repoFor(kind);
        const row = await repo.findOne({
            where: { channel, [channelIdCol]: channelId } as object,
        });
        return row
            ? ((row as unknown as Record<string, string>)[internalCol] ?? null)
            : null;
    }

    async findChannelRef(
        kind: IdentityKind,
        internalId: string,
    ): Promise<{ channel: string; channelId: string } | null> {
        const { repo, internalCol, channelIdCol } = this.repoFor(kind);
        const row = await repo.findOne({
            where: { [internalCol]: internalId } as object,
        });
        if (!row) return null;
        const r = row as unknown as Record<string, string>;
        return { channel: r.channel!, channelId: r[channelIdCol]! };
    }

    async upsertMapping(row: IdentityRow): Promise<string> {
        const { repo, internalCol, channelIdCol, forwardConstraint } =
            this.repoFor(row.kind);

        // 快路径：先 findOne 看 forward-key 是否已存在。命中即幂等返回（最常见
        // 的并发场景：第一个 resolve 把行写进去，后续都从这里返回）。
        const existing = await repo.findOne({
            where: { channel: row.channel, [channelIdCol]: row.channelId } as object,
        });
        if (existing) {
            return (existing as unknown as Record<string, string>)[internalCol]!;
        }

        // 未命中：尝试 save。race 输给并发竞争者会撞 23505：
        //   - 撞 forward-key 唯一约束 -> race winner 已写入 -> 重读返回 winner
        //     的 internalId（绝不抛错，绝不写入 candidate 的 ULID）
        //   - 撞主键 (internal_*_id) 唯一约束 -> 极罕见 ULID 撞 -> 抛
        //     PrimaryKeyConflictError 让 resolver 换 ULID 重试
        const entity = {
            [internalCol]: row.internalId,
            channel: row.channel,
            [channelIdCol]: row.channelId,
        };
        try {
            const saved = (await repo.save(entity as object)) as unknown as Record<string, string>;
            return saved[internalCol]!;
        } catch (err) {
            const d = pgError(err);
            if (d?.code === PG_UNIQUE_VIOLATION) {
                if (d.constraint === forwardConstraint) {
                    // race 输了：另一个并发已经写入同 (channel, channelId)。
                    // 重读拿现有 internalId 返回——收敛到 race winner，不抛错。
                    const winner = await repo.findOne({
                        where: { channel: row.channel, [channelIdCol]: row.channelId } as object,
                    });
                    if (winner) {
                        return (winner as unknown as Record<string, string>)[internalCol]!;
                    }
                    // 理论不可达：23505 forward 约束触发但回读不到说明 store 不
                    // 一致（PG 出了别的问题）。明确抛错而不是吞掉。
                    throw new Error(
                        `identity ${row.kind} forward-key conflict on (${row.channel}, ` +
                            `${row.channelId}) but re-read returned no row; store is inconsistent`,
                    );
                }
                // 23505 但不是 forward-key 约束 -> 是 internal_*_id 主键 (ULID)
                // 冲突。抛 PrimaryKeyConflictError 让 resolver 换 ULID 重试，
                // 绝不当 forward-key 冲突静默收敛到别人的映射。
                throw new PrimaryKeyConflictError(row.kind, row.internalId);
            }
            throw err;
        }
    }
}
