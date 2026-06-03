import AppDataSource from 'ormconfig';
import { LarkGroupMember } from '@entities/lark-group-member';
import { LarkUser } from '@entities/lark-user';
import { getLarkBotMentionAliases } from './bot-identity';

interface GroupMemberInfo {
    union_id: string;
    name: string;
}

const cache = new Map<string, { members: GroupMemberInfo[]; ts: number }>();
const CACHE_TTL_MS = 60_000;

async function getGroupMembers(chatId: string): Promise<GroupMemberInfo[]> {
    const cached = cache.get(chatId);
    if (cached && Date.now() - cached.ts < CACHE_TTL_MS) {
        return cached.members;
    }

    const members = await AppDataSource.getRepository(LarkGroupMember)
        .createQueryBuilder('m')
        .innerJoin(LarkUser, 'u', 'u.union_id = m.union_id')
        .select(['m.union_id AS union_id', 'u.name AS name'])
        .where('m.chat_id = :chatId', { chatId })
        .andWhere('m.is_leave = false')
        .getRawMany<GroupMemberInfo>();

    // 按 name 长度降序，避免短名误匹配长名子串。
    members.sort((a, b) => b.name.length - a.name.length);

    cache.set(chatId, { members, ts: Date.now() });
    return members;
}

function mentionCandidates(members: GroupMemberInfo[]): GroupMemberInfo[] {
    const seen = new Set<string>();
    return [...members, ...getLarkBotMentionAliases()]
        .filter(({ union_id, name }) => {
            const key = `${union_id}\0${name}`;
            if (seen.has(key)) return false;
            seen.add(key);
            return name.length > 0;
        })
        .sort((a, b) => b.name.length - a.name.length);
}

export async function resolveLarkMentionsForGroup(
    content: string,
    chatId: string,
): Promise<string> {
    const members = mentionCandidates(await getGroupMembers(chatId));
    if (members.length === 0) return content;

    let result = content;
    for (const { union_id, name } of members) {
        result = result.replaceAll(`@${name}`, `<at user_id="${union_id}">${name}</at>`);
    }
    return result;
}
