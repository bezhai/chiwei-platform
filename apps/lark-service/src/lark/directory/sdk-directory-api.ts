import type { LarkClientPool } from '../outbound/sdk-lark-api';
import type { LarkDirectoryApi, LarkDirectoryProfile } from './directory';
import {
    DIRECTORY_REQUEST_TIMEOUT_MS,
    withDirectoryDeadline,
    throwIfDirectoryDeadlineExpired,
} from './request-timeout';

function object(value: unknown, label: string): Record<string, unknown> {
    if (!value || typeof value !== 'object' || Array.isArray(value))
        throw new Error(`invalid lark ${label}`);
    return value as Record<string, unknown>;
}

function nonempty(value: unknown, label: string): string {
    if (typeof value !== 'string' || !value.trim())
        throw new Error(`invalid lark ${label}`);
    return value;
}

/** Uses the same bot-scoped client pool as outbound; no additional token cache. */
export function createSdkLarkDirectoryApi(
    pool: LarkClientPool,
    timeoutMs = DIRECTORY_REQUEST_TIMEOUT_MS,
): LarkDirectoryApi {
    return {
        async members(chatId) {
            return withDirectoryDeadline(async () => {
                const client = pool.current();
                const members = new Map<string, LarkDirectoryProfile>();
                const cursors = new Set<string>();
                let cursor: string | undefined;
                while (true) {
                    throwIfDirectoryDeadlineExpired();
                    const page = object(
                        await client.getChatMembers(chatId, cursor, 'union_id'),
                        'member page',
                    );
                    if (
                        !Array.isArray(page.items) ||
                        typeof page.has_more !== 'boolean'
                    )
                        throw new Error('incomplete lark member page');
                    for (const value of page.items) {
                        const item = object(value, 'member');
                        if (item.member_id_type !== 'union_id')
                            throw new Error('unexpected lark member ID type');
                        const unionId = nonempty(
                            item.member_id,
                            'member union_id',
                        );
                        const name = nonempty(item.name, 'member name');
                        if (members.has(unionId))
                            throw new Error(`duplicate lark member ${unionId}`);
                        members.set(unionId, { unionId, name });
                    }
                    if (!page.has_more) break;
                    cursor = nonempty(page.page_token, 'member cursor');
                    if (cursors.has(cursor))
                        throw new Error('repeated lark member cursor');
                    cursors.add(cursor);
                }
                return [...members.values()];
            }, timeoutMs);
        },

        async user(unionId) {
            return withDirectoryDeadline(async () => {
                const response = object(
                    await pool.current().getUserInfo(unionId, 'union_id'),
                    'user response',
                );
                const user = object(response.user, 'user profile');
                if (user.union_id !== unionId)
                    throw new Error(
                        'lark user profile union_id does not match request',
                    );
                const profile: LarkDirectoryProfile = {
                    unionId,
                    name: nonempty(user.name, 'user name'),
                };
                if (user.open_id !== undefined)
                    profile.openId = nonempty(user.open_id, 'user open_id');
                if (user.avatar !== undefined) {
                    const avatar = object(user.avatar, 'user avatar');
                    if (avatar.avatar_origin !== undefined)
                        profile.avatarOrigin = nonempty(
                            avatar.avatar_origin,
                            'user avatar_origin',
                        );
                }
                return profile;
            }, timeoutMs);
        },
    };
}
