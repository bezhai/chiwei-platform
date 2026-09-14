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
}
const memberOf = (row: MemberRow): LarkDirectoryMember => ({
    unionId: row.union_id,
    name: row.name,
    hasLeft: row.is_leave === true,
});

function tablesOn(manager: EntityManager): LarkDirectoryTables {
    return {
        async profile(unionId) {
            const rows = await manager.query<
                Array<{ union_id: string; name: string; avatar_origin: string | null }>
            >('SELECT union_id, name, avatar_origin FROM lark_user WHERE union_id = $1', [unionId]);
            const row = rows[0];
            return row
                ? {
                      unionId: row.union_id,
                      name: row.name,
                      ...(row.avatar_origin ? { avatarOrigin: row.avatar_origin } : {}),
                  }
                : null;
        },
        async member(chatId, unionId) {
            const rows = await manager.query<MemberRow[]>(
                'SELECT m.union_id, u.name, m.is_leave FROM lark_group_member m LEFT JOIN lark_user u ON u.union_id = m.union_id WHERE m.chat_id = $1 AND m.union_id = $2',
                [chatId, unionId],
            );
            return rows[0] ? memberOf(rows[0]) : null;
        },
        async members(chatId) {
            const rows = await manager.query<MemberRow[]>(
                'SELECT m.union_id, u.name, m.is_leave FROM lark_group_member m LEFT JOIN lark_user u ON u.union_id = m.union_id WHERE m.chat_id = $1 ORDER BY m.union_id',
                [chatId],
            );
            return rows.map(memberOf);
        },
        async humanUnionIds(chatId) {
            const rows = await manager.query<Array<{ sender_union_id: string }>>(
                "SELECT DISTINCT sender_union_id FROM lark_message WHERE chat_id = $1 AND sender_union_id IS NOT NULL AND raw_event->'sender'->>'sender_type' = 'user'",
                [chatId],
            );
            return rows.map((row) => row.sender_union_id);
        },
        async saveProfile(profile: LarkDirectoryProfile) {
            // Only names/avatar belong to the directory. Never replace permission flags.
            await manager.query(
                'INSERT INTO lark_user (union_id, name, avatar_origin) VALUES ($1, $2, $3) ON CONFLICT (union_id) DO UPDATE SET name = EXCLUDED.name, avatar_origin = CASE WHEN $4 THEN EXCLUDED.avatar_origin ELSE lark_user.avatar_origin END',
                [
                    profile.unionId,
                    profile.name,
                    profile.avatarOrigin ?? null,
                    profile.avatarOrigin !== undefined,
                ],
            );
            await manager.query(
                'UPDATE lark_user_open_id SET name = $2 WHERE union_id = $1 AND name IS DISTINCT FROM $2',
                [profile.unionId, profile.name],
            );
            await manager.query(
                'UPDATE common_user u SET display_name = $2 FROM lark_user_open_id l WHERE l.union_id = $1 AND l.common_user_id = u.common_user_id AND u.display_name IS DISTINCT FROM $2',
                [profile.unionId, profile.name],
            );
        },
        async applyMembers(chatId, present, left) {
            if (present.length)
                await manager.query(
                    'INSERT INTO lark_group_member (chat_id, union_id, is_leave, created_at, updated_at) SELECT $1, unnest($2::text[]), false, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP ON CONFLICT (chat_id, union_id) DO UPDATE SET is_leave = false, updated_at = CURRENT_TIMESTAMP',
                    [chatId, present.map((person) => person.unionId)],
                );
            if (left.length)
                await manager.query(
                    'UPDATE lark_group_member SET is_leave = true, updated_at = CURRENT_TIMESTAMP WHERE chat_id = $1 AND union_id = ANY($2::text[])',
                    [chatId, [...left]],
                );
        },
    };
}

export function postgresLarkDirectoryStore(dataSource: DataSource): LarkDirectoryStore {
    const transaction = <T>(run: (manager: EntityManager) => Promise<T>) =>
        dataSource.transaction(async (manager) => {
            await manager.query("SET LOCAL lock_timeout = '5s'");
            await manager.query("SET LOCAL statement_timeout = '10s'");
            await manager.query("SET LOCAL idle_in_transaction_session_timeout = '30s'");
            return run(manager);
        });
    return {
        ...tablesOn(dataSource.manager),
        // Private-chat profile updates also keep all three names in one transaction.
        saveProfile: (profile) => transaction((manager) => tablesOn(manager).saveProfile(profile)),
        withChatLock: (chatId, run) =>
            transaction(async (manager) => {
                // Transaction-scoped advisory lock has no expiring lease. All writers use
                // the same chat key, including multiple bots and different service pods.
                await manager.query('SELECT pg_advisory_xact_lock(hashtextextended($1, 0))', [
                    `lark-directory:${chatId}`,
                ]);
                return run(tablesOn(manager));
            }),
    };
}
