// 生产运行时的 IdentityStore 实现：用 TypeORM 读写 identity_user /
// identity_conversation / identity_message 三张映射表。写路径用真实的
// INSERT ... ON CONFLICT (channel, channel_*_id) DO UPDATE ... RETURNING，
// 把 forward-key 冲突交给 PG 引擎在单条语句内收敛（事务安全、不依赖隔离
// 级别——DO UPDATE 永远 RETURNING，不需要 CTE/UNION ALL 兜底；之前用 DO
// NOTHING + UNION ALL SELECT 在 read committed 下 race 时 SELECT 看不到
// conflicting row，会返回 0 行让上游 fail-loud）。只把 internal_*_id 主键
// (UUIDv7) 冲突翻译成 PrimaryKeyConflictError 让 DbIdentityResolver 换 UUIDv7
// 重试。
//
// 注意：IdentityKind 的 'chat' 仍是会话命名空间的判别 key（resolver 契约未变），
// 但它在 DB 侧落到的表/列/约束已统一改成 conversation 命名（见 KIND_TABLE.chat）。
//
// 单测不 import 本文件（它静态依赖 TypeORM 数据源）；DbIdentityResolver 的
// 单测走内存版 FakeIdentityStore。本文件只在运行时接线时使用。

import { QueryFailedError } from 'typeorm';
import AppDataSource from '@ormconfig';
import {
    IdentityUser,
    IdentityConversation,
    IdentityMessage,
} from '@entities/identity-mapping';
import type { IdentityKind } from '@core/channels/identity-resolver';
import {
    type IdentityStore,
    type IdentityRow,
    PrimaryKeyConflictError,
} from '@core/channels/db-identity-resolver';

// 每个 kind 对应哪张表、哪两个列、表名、约束名。三张表结构相同，
// 只是列名/约束名带各自前缀。约束名必须与 DDL（channel-layer-identity-
// mapping-tables.sql）一致，ON CONFLICT 按约束名引用。
// 注：IdentityKind 的 'chat' 是会话命名空间判别 key，DB 侧落 conversation 命名。
const KIND_TABLE = {
    user: {
        entity: IdentityUser,
        table: 'identity_user',
        internalCol: 'internal_user_id',
        channelIdCol: 'channel_user_id',
        forwardConstraint: 'uq_identity_user_channel',
    },
    chat: {
        entity: IdentityConversation,
        table: 'identity_conversation',
        internalCol: 'internal_conversation_id',
        channelIdCol: 'channel_conversation_id',
        forwardConstraint: 'uq_identity_conversation_channel',
    },
    message: {
        entity: IdentityMessage,
        table: 'identity_message',
        internalCol: 'internal_message_id',
        channelIdCol: 'channel_message_id',
        forwardConstraint: 'uq_identity_message_channel',
    },
} as const;

// PG 唯一约束违反 sqlstate。
const PG_UNIQUE_VIOLATION = '23505';

// 把 driverError 拆出来：PG 在 23505 时附带 constraint（违反的约束名），
// 用它区分 forward-key 冲突 vs 主键(UUIDv7)冲突。
function pgError(
    err: unknown,
): { code?: string; constraint?: string } | null {
    if (!(err instanceof QueryFailedError)) return null;
    const d = (
        err as unknown as {
            driverError?: { code?: string; constraint?: string };
        }
    ).driverError;
    return d ?? null;
}

export class TypeOrmIdentityStore implements IdentityStore {
    async findInternalId(
        kind: IdentityKind,
        channel: string,
        channelId: string,
    ): Promise<string | null> {
        const t = KIND_TABLE[kind];
        const repo = AppDataSource.getRepository(t.entity);
        const row = await repo.findOne({
            where: { channel, [t.channelIdCol]: channelId } as object,
        });
        return row
            ? ((row as unknown as Record<string, string>)[t.internalCol] ??
                  null)
            : null;
    }

    async findChannelRef(
        kind: IdentityKind,
        internalId: string,
    ): Promise<{ channel: string; channelId: string } | null> {
        const t = KIND_TABLE[kind];
        const repo = AppDataSource.getRepository(t.entity);
        const row = await repo.findOne({
            where: { [t.internalCol]: internalId } as object,
        });
        if (!row) return null;
        const r = row as unknown as Record<string, string>;
        return { channel: r.channel!, channelId: r[t.channelIdCol]! };
    }

    async upsertMapping(row: IdentityRow): Promise<string> {
        const t = KIND_TABLE[row.kind];
        // 单 SQL upsert：对 forward-key 复合唯一约束 ON CONFLICT DO UPDATE。
        // 之前用 DO NOTHING + 同 CTE UNION ALL SELECT 回取，PG read committed
        // 下当 DO NOTHING 触发时同语句的 SELECT 看不到 conflicting row（快照
        // 边界），并发同 (channel, channel_*_id) 第二次进来 SELECT 0 行 → 整体
        // 0 行 → 调用方 fail-loud → prod 丢消息。
        //
        // DO UPDATE 即使 SET 为 no-op（把 channel_*_id 设回 EXCLUDED.* 同值）
        // 仍会"涉及行"，把那条行通过 RETURNING 拉回来，永远拿到 internal_id；
        // forward-key 在 PG 引擎单语句内收敛，不依赖外层事务/隔离级别。
        // 注意：永远不要 SET internal_*_id —— 那是全局主键，DO UPDATE 时
        // 已存在行的 internal_id 不能被来访 candidate UUIDv7 覆盖。
        const sql = `
            INSERT INTO ${t.table} (${t.internalCol}, channel, ${t.channelIdCol})
            VALUES ($1, $2, $3)
            ON CONFLICT ON CONSTRAINT ${t.forwardConstraint}
            DO UPDATE SET ${t.channelIdCol} = EXCLUDED.${t.channelIdCol}
            RETURNING ${t.internalCol} AS internal_id
        `;
        try {
            const rows = (await AppDataSource.query(sql, [
                row.internalId,
                row.channel,
                row.channelId,
            ])) as Array<{ internal_id: string }>;
            const internalId = rows[0]?.internal_id;
            if (internalId == null) {
                // 理论不可达：要么 INSERT 成功 RETURNING，要么 DO NOTHING
                // 后 SELECT 命中已存在行。读不到说明 store 状态不一致。
                throw new Error(
                    `identity ${row.kind} upsert (${row.channel}, ${row.channelId}) ` +
                        `returned no internal id; store is inconsistent`,
                );
            }
            return internalId;
        } catch (err) {
            const d = pgError(err);
            if (d?.code === PG_UNIQUE_VIOLATION) {
                // 23505 但不是 forward-key 约束 -> 是 internal_*_id 主键(UUIDv7)
                // 冲突。区分二者：forward-key 冲突已被 ON CONFLICT DO UPDATE
                // 吸收不会冒到这里；冒到这里的 23505 即主键冲突，翻译成
                // PrimaryKeyConflictError 让 resolver 换 UUIDv7 重试。
                if (d.constraint !== t.forwardConstraint) {
                    throw new PrimaryKeyConflictError(
                        row.kind,
                        row.internalId,
                    );
                }
            }
            throw err;
        }
    }
}
