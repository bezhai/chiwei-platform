import type { LarkMessageEvent } from '../message/wire';

export interface LarkDirectoryProfile {
    unionId: string;
    name: string;
    openId?: string;
    avatarOrigin?: string;
}

export interface LarkDirectoryMember {
    unionId: string;
    name: string | null;
    hasLeft: boolean;
    /** Legacy values are a conservative state boundary, not necessarily event times. */
    updatedAt: Date | null;
}

export interface LarkDirectoryApi {
    user(unionId: string): Promise<LarkDirectoryProfile>;
}

export interface LarkDirectoryTables {
    profile(unionId: string): Promise<LarkDirectoryProfile | null>;
    member(
        chatId: string,
        unionId: string,
    ): Promise<LarkDirectoryMember | null>;
    /** Atomically fill a missing/blank name; never replace a nonempty profile. */
    fillProfile(profile: LarkDirectoryProfile): Promise<void>;
    /** Apply newer evidence atomically; leaving wins a timestamp tie. */
    applyMembership(
        chatId: string,
        unionId: string,
        hasLeft: boolean,
        observedAt: Date,
    ): Promise<boolean>;
}

export interface LarkDirectoryStore extends LarkDirectoryTables {
    /** Transaction and cross-process lock cover the callback's reads and writes. */
    withChatLock<T>(
        chatId: string,
        run: (tables: LarkDirectoryTables) => Promise<T>,
    ): Promise<T>;
}

export interface LarkDirectory {
    changeMembers(
        chatId: string,
        members: readonly { unionId: string; name: string }[],
        hasLeft: boolean,
        observedAt: Date,
    ): Promise<void>;
    /** True means projection should reread profile or membership facts. Throws on failure. */
    ensureSender(event: LarkMessageEvent): Promise<boolean>;
}

function completeProfile(profile: LarkDirectoryProfile | null): boolean {
    return profile !== null && profile.name.trim().length > 0;
}

function validateTime(time: Date): void {
    if (!Number.isFinite(time.getTime()) || time.getTime() < 0)
        throw new Error('invalid lark membership evidence time');
}

export function createLarkDirectory(deps: {
    store: LarkDirectoryStore;
    api: LarkDirectoryApi;
    botUnionIds?: readonly string[];
}): LarkDirectory {
    const bots = new Set(deps.botUnionIds ?? []);
    return {
        async changeMembers(chatId, members, hasLeft, observedAt) {
            validateTime(observedAt);
            const humans = members.filter(
                (member) => !bots.has(member.unionId),
            );
            for (const member of humans) {
                if (!member.unionId || (!hasLeft && !member.name.trim()))
                    throw new Error('invalid lark member identity');
            }
            if (!humans.length) return;
            // Profiles are shared across chats. Lock their rows in a stable order.
            const ordered = [...humans].sort((a, b) =>
                a.unionId.localeCompare(b.unionId),
            );
            await deps.store.withChatLock(chatId, async (tables) => {
                for (const member of ordered) {
                    await tables.applyMembership(
                        chatId,
                        member.unionId,
                        hasLeft,
                        observedAt,
                    );
                    // Membership event times are local to each chat. Without a global
                    // name version, events may only fill names that are still missing.
                    if (!hasLeft) await tables.fillProfile(member);
                }
            });
        },

        async ensureSender(event) {
            if (event.sender.sender_type !== 'user') return false;
            const unionId = event.sender.sender_id?.union_id;
            if (!unionId || bots.has(unionId)) return false;
            const profile = await deps.store.profile(unionId);
            if (event.message.chat_type === 'p2p') {
                if (completeProfile(profile)) return false;
                await deps.store.fillProfile(await deps.api.user(unionId));
                return true;
            }
            if (!/^\d+$/.test(event.message.create_time))
                throw new Error('invalid lark message evidence time');
            const observedAt = new Date(Number(event.message.create_time));
            validateTime(observedAt);
            const chatId = event.message.chat_id;
            // This commit must survive a later contact lookup failure. Every message
            // advances evidence, even if the sender already has a complete profile.
            const membershipChanged = await deps.store.withChatLock(
                chatId,
                async (tables) => {
                    const previous = await tables.member(chatId, unionId);
                    const applied = await tables.applyMembership(
                        chatId,
                        unionId,
                        false,
                        observedAt,
                    );
                    return applied && (!previous || previous.hasLeft);
                },
            );
            if (completeProfile(profile)) return membershipChanged;
            // Contact latency must not hold this chat's transaction lock. An event
            // may supply a newer name while the request is in flight.
            const fetchedProfile = await deps.api.user(unionId);
            await deps.store.withChatLock(chatId, async (tables) => {
                if (!completeProfile(await tables.profile(unionId)))
                    await tables.fillProfile(fetchedProfile);
            });
            return true;
        },
    };
}
