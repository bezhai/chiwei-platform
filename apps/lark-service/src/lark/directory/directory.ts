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
}

export interface LarkDirectoryApi {
    /** Returns all human members only after every page has been validated. */
    members(chatId: string): Promise<readonly LarkDirectoryProfile[]>;
    user(unionId: string): Promise<LarkDirectoryProfile>;
}

export interface LarkDirectoryTables {
    profile(unionId: string): Promise<LarkDirectoryProfile | null>;
    member(chatId: string, unionId: string): Promise<LarkDirectoryMember | null>;
    members(chatId: string): Promise<readonly LarkDirectoryMember[]>;
    /** Human identity evidence from real user messages in this chat. */
    humanUnionIds(chatId: string): Promise<readonly string[]>;
    saveProfile(profile: LarkDirectoryProfile): Promise<void>;
    applyMembers(
        chatId: string,
        present: readonly LarkDirectoryProfile[],
        left: readonly string[],
    ): Promise<void>;
}

export interface LarkDirectoryStore extends LarkDirectoryTables {
    /** Transaction and cross-process lock cover API reads as well as writes. */
    withChatLock<T>(chatId: string, run: (tables: LarkDirectoryTables) => Promise<T>): Promise<T>;
}

export interface LarkDirectorySyncResult {
    chatId: string;
    preview: boolean;
    members: Array<{ unionId: string; name: string }>;
    joined: Array<{ unionId: string; name: string }>;
    left: Array<{ unionId: string; name: string | null }>;
    renamed: Array<{ unionId: string; before: string | null; after: string }>;
}

export interface LarkDirectory {
    sync(
        chatId: string,
        options: { preview: boolean; humanUnionIds: readonly string[] },
    ): Promise<LarkDirectorySyncResult>;
    /** Throws on API/storage failure; the message entry point decides whether to continue. */
    ensureSender(event: LarkMessageEvent): Promise<boolean>;
}

function completeProfile(profile: LarkDirectoryProfile | null): boolean {
    return profile !== null && profile.name.trim().length > 0;
}

export function createLarkDirectory(deps: {
    store: LarkDirectoryStore;
    api: LarkDirectoryApi;
    botUnionIds?: readonly string[];
}): LarkDirectory {
    const bots = new Set(deps.botUnionIds ?? []);
    async function syncLocked(
        tables: LarkDirectoryTables,
        chatId: string,
        options: { preview: boolean; humanUnionIds: readonly string[] },
    ): Promise<LarkDirectorySyncResult> {
        const current = await deps.api.members(chatId);
        const [previous, historicalHumans] = await Promise.all([
            tables.members(chatId),
            tables.humanUnionIds(chatId),
        ]);
        const humans = new Set([...historicalHumans, ...options.humanUnionIds]);
        const previousById = new Map(previous.map((member) => [member.unionId, member]));
        const presentIds = new Set<string>();
        const members: LarkDirectorySyncResult['members'] = [];
        const joined: LarkDirectorySyncResult['joined'] = [];
        const renamed: LarkDirectorySyncResult['renamed'] = [];
        for (const person of current) {
            if (
                !person.unionId ||
                !person.name.trim() ||
                bots.has(person.unionId) ||
                presentIds.has(person.unionId)
            ) {
                throw new Error('invalid human member in lark directory snapshot');
            }
            presentIds.add(person.unionId);
            members.push({ unionId: person.unionId, name: person.name });
            const old = previousById.get(person.unionId);
            if (!old || old.hasLeft) joined.push({ unionId: person.unionId, name: person.name });
            // A user can have a profile but no membership row in this chat.
            const before = old?.name ?? (await tables.profile(person.unionId))?.name ?? null;
            if (before !== person.name)
                renamed.push({
                    unionId: person.unionId,
                    before,
                    after: person.name,
                });
        }
        const left = previous
            .filter(
                (member) =>
                    !member.hasLeft &&
                    !presentIds.has(member.unionId) &&
                    humans.has(member.unionId) &&
                    !bots.has(member.unionId),
            )
            .map((member) => ({ unionId: member.unionId, name: member.name }));
        if (!options.preview) {
            // Users are shared across chats: take their row locks in a stable
            // order even when two different group snapshots arrive concurrently.
            const ordered = [...current].sort((a, b) => a.unionId.localeCompare(b.unionId));
            for (const person of ordered) await tables.saveProfile(person);
            await tables.applyMembers(
                chatId,
                current,
                left.map((member) => member.unionId),
            );
        }
        return {
            chatId,
            preview: options.preview,
            members,
            joined,
            left,
            renamed,
        };
    }
    const directory: LarkDirectory = {
        async sync(chatId, options) {
            return deps.store.withChatLock(chatId, async (tables) => {
                return syncLocked(tables, chatId, options);
            });
        },

        async ensureSender(event) {
            if (event.sender.sender_type !== 'user') return false;
            const unionId = event.sender.sender_id?.union_id;
            if (!unionId || bots.has(unionId)) return false;
            let profile = await deps.store.profile(unionId);
            if (event.message.chat_type === 'p2p') {
                if (completeProfile(profile)) return false;
                await deps.store.saveProfile(await deps.api.user(unionId));
                return true;
            }
            const member = await deps.store.member(event.message.chat_id, unionId);
            if (completeProfile(profile) && member && !member.hasLeft) return false;
            await deps.store.withChatLock(event.message.chat_id, async (tables) => {
                const [latestProfile, latestMember] = await Promise.all([
                    tables.profile(unionId),
                    tables.member(event.message.chat_id, unionId),
                ]);
                // Another message may have completed this user's synchronization while
                // this one waited. The caller still needs to reread its earlier facts.
                if (completeProfile(latestProfile) && latestMember && !latestMember.hasLeft) return;
                await syncLocked(tables, event.message.chat_id, {
                    preview: false,
                    humanUnionIds: [unionId],
                });
            });
            profile = await deps.store.profile(unionId);
            // Replayed messages may belong to someone who has since left. Get their name
            // separately, without manufacturing a current membership from an old message.
            if (!completeProfile(profile))
                await deps.store.saveProfile(await deps.api.user(unionId));
            return true;
        },
    };
    return directory;
}
