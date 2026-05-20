import { LarkGroupChatInfo, LarkGroupMember } from 'infrastructure/dal/entities';
import {
    GroupChatInfoRepository,
    GroupMemberRepository,
    LarkUserOpenIdRepository,
    UserRepository,
} from 'infrastructure/dal/repositories/repositories';
import { setTimeout } from 'timers/promises';
import { searchAllLarkGroup, searchLarkChatInfo, searchLarkChatMember } from '@lark/basic/group';

export async function upsertAllChatInfo() {
    const chatList = await searchAllLarkGroup();
    const groupInfoList: LarkGroupChatInfo[] = [];
    const membersList: LarkGroupMember[] = [];

    for (const chatId of chatList) {
        console.info(`upsert chat ${chatId}`);
        const { groupInfo, members } = await searchLarkChatInfo(chatId);
        groupInfoList.push(groupInfo);
        membersList.push(...members);
        const { users, members: newMembers, openIdUsers } = await searchLarkChatMember(chatId);
        await Promise.all([
            GroupMemberRepository.save(newMembers),
            UserRepository.save(users),
            LarkUserOpenIdRepository.save(openIdUsers),
        ]); // 这里每个群更新一次, 防止数据量过大
        await setTimeout(200);
    }
    await Promise.all([
        GroupChatInfoRepository.save(groupInfoList),
        GroupMemberRepository.save(membersList),
    ]);
}
