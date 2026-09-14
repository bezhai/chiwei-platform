import type { DataSource, EntityManager } from 'typeorm';
import type {
    LarkDirectoryMember,
    LarkDirectoryProfile,
    LarkDirectoryStore,
    LarkDirectoryTables,
} from './directory';

interface MemberRow {
    union_id: string;
    name: string | null;
    is_leave: boolean | null;
    updated_at: Date | null;
}
const memberOf = (row: MemberRow): LarkDirectoryMember => ({
    unionId: row.union_id,
    name: row.name,
    hasLeft: row.is_leave === true,
    updatedAt: row.updated_at,
});

function tablesOn(manager: EntityManager): LarkDirectoryTables {
    return {
        async profile(unionId) {
            const rows = await manager.query<
                Array<{
                    union_id: string;
                    name: string;
                    avatar_origin: string | null;
                }>
            >(
                'SELECT union_id, name, avatar_origin FROM lark_user WHERE union_id = $1',
                [unionId],
            );
            const row = rows[0];
            return row
                ? {
                      unionId: row.union_id,
                      name: row.name,
                      ...(row.avatar_origin
                          ? { avatarOrigin: row.avatar_origin }
                          : {}),
                  }
                : null;
        },
        async member(chatId, unionId) {
            const rows = await manager.query<MemberRow[]>(
                "SELECT m.union_id, u.name, m.is_leave, m.updated_at AT TIME ZONE 'UTC' AS updated_at FROM lark_group_member m LEFT JOIN lark_user u ON u.union_id = m.union_id WHERE m.chat_id = $1 AND m.union_id = $2",
                [chatId, unionId],
            );
            return rows[0] ? memberOf(rows[0]) : null;
        },
        async fillProfile(profile: LarkDirectoryProfile) {
            // Only fill missing names; late events from another chat must not rename users.
            const inserted = await manager.query<Array<{union_id: string}>>(
                'INSERT INTO lark_user (union_id, name, avatar_origin) VALUES ($1, $2, $3) ON CONFLICT (union_id) DO UPDATE SET name = EXCLUDED.name, avatar_origin = CASE WHEN $4 THEN EXCLUDED.avatar_origin ELSE lark_user.avatar_origin END WHERE lark_user.name IS NULL OR btrim(lark_user.name) = \'\' RETURNING union_id',
                [
                    profile.unionId,
                    profile.name,
                    profile.avatarOrigin ?? null,
                    profile.avatarOrigin !== undefined,
                ],
            );
            if (!inserted.length) return;
            await manager.query(
                'UPDATE lark_user_open_id SET name = $2 WHERE union_id = $1 AND name IS DISTINCT FROM $2',
                [profile.unionId, profile.name],
            );
            await manager.query(
                'UPDATE common_user u SET display_name = $2 FROM lark_user_open_id l WHERE l.union_id = $1 AND l.common_user_id = u.common_user_id AND u.display_name IS DISTINCT FROM $2',
                [profile.unionId, profile.name],
            );
        },
        async applyMembership(chatId, unionId, hasLeft, observedAt) {
            const rows = await manager.query<Array<{ union_id: string }>>(
                `INSERT INTO lark_group_member (chat_id, union_id, is_leave, created_at, updated_at)
                 VALUES ($1, $2, $3, CURRENT_TIMESTAMP AT TIME ZONE 'UTC', $4::timestamptz AT TIME ZONE 'UTC')
                 ON CONFLICT (chat_id, union_id) DO UPDATE
                 SET is_leave = EXCLUDED.is_leave, updated_at = EXCLUDED.updated_at
                 WHERE lark_group_member.updated_at IS NULL
                    OR lark_group_member.updated_at < EXCLUDED.updated_at
                    OR (lark_group_member.updated_at = EXCLUDED.updated_at
                        AND EXCLUDED.is_leave = true
                        AND lark_group_member.is_leave IS DISTINCT FROM true)
                 RETURNING union_id`,
                [chatId, unionId, hasLeft, observedAt],
            );
            return rows.length > 0;
        },
    };
}

export function postgresLarkDirectoryStore(
    dataSource: DataSource,
): LarkDirectoryStore {
    const transaction = <T>(run: (manager: EntityManager) => Promise<T>) =>
        dataSource.transaction(async (manager) => {
            await manager.query("SET LOCAL lock_timeout = '5s'");
            await manager.query("SET LOCAL statement_timeout = '10s'");
            await manager.query(
                "SET LOCAL idle_in_transaction_session_timeout = '30s'",
            );
            return run(manager);
        });
    return {
        ...tablesOn(dataSource.manager),
        // Private-chat profile updates also keep all three names in one transaction.
        fillProfile: (profile) =>
            transaction((manager) => tablesOn(manager).fillProfile(profile)),
        withChatLock: (chatId, run) =>
            transaction(async (manager) => {
                // Transaction-scoped advisory lock has no expiring lease. All writers use
                // the same chat key, including multiple bots and different service pods.
                await manager.query(
                    'SELECT pg_advisory_xact_lock(hashtextextended($1, 0))',
                    [`lark-directory:${chatId}`],
                );
                return run(tablesOn(manager));
            }),
    };
}
