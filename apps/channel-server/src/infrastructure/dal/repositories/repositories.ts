// 本服务自己用的仓储入口。共享能力自己要读写的表（bot_config /
// user_blacklist / common_agent_response）的仓储在 @inner/shared/persistence，
// 不在这里重复一份。

import AppDataSource from 'ormconfig';
import {
    LarkEmoji,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkUser,
    LarkUserOpenId,
    LarkMessage,
} from '@entities';
import { CommonMessage } from '@inner/shared/entities';
import { UserGroupBindingRepository as CustomUserGroupBindingRepository } from './user-group-binding-repository';

export const LarkEmojiRepository = AppDataSource.getRepository(LarkEmoji);
export const UserRepository = AppDataSource.getRepository(LarkUser);
export const BaseChatInfoRepository = AppDataSource.getRepository(LarkBaseChatInfo);
export const GroupChatInfoRepository = AppDataSource.getRepository(LarkGroupChatInfo);
export const GroupMemberRepository = AppDataSource.getRepository(LarkGroupMember);
export const LarkUserOpenIdRepository = AppDataSource.getRepository(LarkUserOpenId);

export const UserGroupBindingRepository = new CustomUserGroupBindingRepository(AppDataSource);

export const CommonMessageRepository = AppDataSource.getRepository(CommonMessage);
export const LarkMessageRepository = AppDataSource.getRepository(LarkMessage);
