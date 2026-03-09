import AppDataSource from 'ormconfig';
import { LarkGroupMember } from '@entities/lark-group-member';
import { LarkUser } from '@entities/lark-user';

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

    // 按 name 长度降序，避免短名误匹配长名子串
    members.sort((a, b) => b.name.length - a.name.length);

    cache.set(chatId, { members, ts: Date.now() });
    return members;
}

/**
 * 将 AI 回复中的 @用户名 替换为 <at union_id="xxx">用户名</at>
 */
export async function resolveMentionsForGroup(
    content: string,
    chatId: string,
): Promise<string> {
    const members = await getGroupMembers(chatId);
    if (members.length === 0) return content;

    let result = content;
    for (const { union_id, name } of members) {
        result = result.replaceAll(`@${name}`, `<at user_id="${union_id}">${name}</at>`);
    }
    return result;
}
