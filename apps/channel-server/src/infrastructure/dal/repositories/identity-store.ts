// 生产运行时的 IdentityStore 实现：用 TypeORM 读写 identity_user /
// identity_chat / identity_message 三张映射表。写路径用真实的
// INSERT ... ON CONFLICT (channel, channel_*_id) DO NOTHING + RETURNING /
// 回取，把 forward-key 冲突交给 PG 引擎在单条语句内收敛（事务安全、不依赖
// 隔离级别），只把 internal_*_id 主键(ULID)冲突翻译成
// PrimaryKeyConflictError 让 DbIdentityResolver 换 ULID 重试。
//
// 单测不 import 本文件（它静态依赖 TypeORM 数据源）；DbIdentityResolver 的
// 单测走内存版 FakeIdentityStore。本文件只在运行时接线时使用。

import { QueryFailedError } from 'typeorm';
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

// 每个 kind 对应哪张表、哪两个列、表名、约束名。三张表结构相同，
// 只是列名/约束名带各自前缀。约束名必须与 DDL（multi-channel-T5-
// identity-mapping-tables.sql）一致，ON CONFLICT 按约束名引用。
const KIND_TABLE = {
    user: {
        entity: IdentityUser,
        table: 'identity_user',
        internalCol: 'internal_user_id',
        channelIdCol: 'channel_user_id',
        forwardConstraint: 'uq_identity_user_channel',
    },
    chat: {
        entity: IdentityChat,
        table: 'identity_chat',
        internalCol: 'internal_chat_id',
        channelIdCol: 'channel_chat_id',
        forwardConstraint: 'uq_identity_chat_channel',
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
// 用它区分 forward-key 冲突 vs 主键(ULID)冲突。
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
        // 单 SQL upsert：对 forward-key 复合唯一约束 ON CONFLICT DO NOTHING；
        // DO NOTHING 时 RETURNING 不产出行，故用 CTE 在 INSERT 不命中时回取
        // 已存在行的 internal id，保证恒返回收敛后的 internal id（一条语句、
        // 不依赖外层事务/隔离级别——并发首次出现下 PG 引擎内收敛）。
        const sql = `
            WITH ins AS (
                INSERT INTO ${t.table} (${t.internalCol}, channel, ${t.channelIdCol})
                VALUES ($1, $2, $3)
                ON CONFLICT ON CONSTRAINT ${t.forwardConstraint} DO NOTHING
                RETURNING ${t.internalCol} AS internal_id
            )
            SELECT internal_id FROM ins
            UNION ALL
            SELECT ${t.internalCol} AS internal_id FROM ${t.table}
            WHERE channel = $2 AND ${t.channelIdCol} = $3
            LIMIT 1
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
                // 23505 但不是 forward-key 约束 -> 是 internal_*_id 主键(ULID)
                // 冲突。区分二者：forward-key 冲突已被 ON CONFLICT DO NOTHING
                // 吸收不会冒到这里；冒到这里的 23505 即主键冲突，翻译成
                // PrimaryKeyConflictError 让 resolver 换 ULID 重试。
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
