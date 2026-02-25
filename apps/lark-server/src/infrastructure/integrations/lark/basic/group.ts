import { LarkGroupChatInfo, LarkGroupMember, LarkUser } from 'infrastructure/dal/entities';
import { LarkUserOpenId } from 'infrastructure/dal/entities/lark-user-open-id';
import { getChatList, getChatInfo, searchAllMembers, addChatMember } from '@lark-client';
import { getBotAppId } from '@core/services/bot/bot-var';

// 从飞书获取所有群聊列表
export async function searchAllLarkGroup() {
    const chatIdList: string[] = [];

    let pageToken: string | undefined = undefined;

    while (true) {
        const res = await getChatList(pageToken);

        pageToken = res?.page_token;

        if (res?.items) {
            chatIdList.push(
                ...res.items
                    .filter((item) => item.chat_status == 'normal')
                    .map((item) => item.chat_id!),
            );
        }

        if (!res?.has_more) {
            break;
        }
    }

    return chatIdList;
}

export async function searchLarkChatInfo(chat_id: string) {
    const chatInfo = await getChatInfo(chat_id);

    if (!chatInfo) {
        throw new Error(`chat_id ${chat_id} not found`);
    }

    const groupInfo: LarkGroupChatInfo = {
        name: chatInfo.name!,
        avatar: chatInfo.avatar!,
        description: chatInfo.description!,
        user_manager_id_list: chatInfo.user_manager_id_list!,
        chat_tag: chatInfo.chat_tag!,
        group_message_type: chatInfo.group_message_type as 'chat' | 'thread' | undefined,
        chat_status: chatInfo.chat_status!,
        download_has_permission_setting: chatInfo.restricted_mode_setting
            ?.download_has_permission_setting as 'all_members' | 'not_anyone' | undefined,
        user_count: chatInfo.user_count ? Number(chatInfo.user_count) : 0,
        chat_id,
        baseChatInfo: {
            chat_id,
            chat_mode: chatInfo.chat_mode as 'topic' | 'group',
            // 已移除 has_main_bot 和 has_dev_bot 字段，使用多机器人配置表替代
        },
        is_leave: false,
    };

    const members: LarkGroupMember[] = [
        {
            chat_id,
            union_id: chatInfo.owner_id!,
            is_owner: true,
            created_at: new Date(),
            updated_at: new Date(),
        },
    ];

    members.push(
        ...chatInfo.user_manager_id_list!.map((union_id) => ({
            chat_id,
            union_id,
            is_manager: true,
            created_at: new Date(),
            updated_at: new Date(),
        })),
    );

    return {
        groupInfo,
        members,
    };
}

export async function searchLarkChatMember(chat_id: string) {
    const members: LarkGroupMember[] = [];
    const users: LarkUser[] = [];
    const openIdUsers: LarkUserOpenId[] = [];
    let pageToken: string | undefined = undefined;

    while (true) {
        const res = await searchAllMembers(chat_id, pageToken, 'union_id');
        pageToken = res?.page_token;
        if (res?.items) {
            members.push(
                ...res.items.map((item) => ({
                    chat_id,
                    union_id: item.member_id!,
                    created_at: new Date(),
                    updated_at: new Date(),
                })),
            );
            users.push(
                ...res.items.map((item) => ({
                    union_id: item.member_id!,
                    name: item.name!,
                })),
            );
        }
        if (!res?.has_more) {
            break;
        }
    }

    pageToken = undefined;
    while (true) {
        const res = await searchAllMembers(chat_id, pageToken, 'open_id');
        pageToken = res?.page_token;
        if (res?.items) {
            openIdUsers.push(
                ...res.items.map((item) => ({
                    name: item.name!,
                    appId: getBotAppId(),
                    openId: item.member_id!,
                })),
            );
        }
        if (!res?.has_more) {
            break;
        }
    }

    return {
        members,
        users,
        openIdUsers,
    };
}

export { addChatMember };
