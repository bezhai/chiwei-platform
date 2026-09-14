import type { LarkClientPool } from '../outbound/sdk-lark-api';
import type { LarkDirectoryApi, LarkDirectoryProfile } from './directory';
import {
    DIRECTORY_REQUEST_TIMEOUT_MS,
    withDirectoryDeadline,
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
