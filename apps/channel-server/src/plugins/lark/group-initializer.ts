import { LarkGroupChatInfo, LarkGroupMember } from 'infrastructure/dal/entities';
import {
    GroupChatInfoRepository,
    GroupMemberRepository,
    LarkUserOpenIdRepository,
    UserRepository,
} from 'infrastructure/dal/repositories/repositories';
import { setTimeout } from 'timers/promises';
import {
    searchAllLarkGroup,
    searchLarkChatInfo,
    searchLarkChatMember,
} from '@lark/basic/group';

export async function upsertAllLarkChatInfo(): Promise<void> {
    const chatList = await searchAllLarkGroup();
    const groupInfoList: LarkGroupChatInfo[] = [];
    const membersList: LarkGroupMember[] = [];

    for (const chatId of chatList) {
        console.info(`upsert lark chat ${chatId}`);
        const { groupInfo, members } = await searchLarkChatInfo(chatId);
        groupInfoList.push(groupInfo);
        membersList.push(...members);
        const { users, members: newMembers, openIdUsers } =
            await searchLarkChatMember(chatId);
        await Promise.all([
            GroupMemberRepository.save(newMembers),
            UserRepository.save(users),
            LarkUserOpenIdRepository.save(openIdUsers),
        ]);
        await setTimeout(200);
    }
    await Promise.all([
        GroupChatInfoRepository.save(groupInfoList),
        GroupMemberRepository.save(membersList),
    ]);
}
