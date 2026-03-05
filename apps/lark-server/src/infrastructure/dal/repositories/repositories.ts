import AppDataSource from 'ormconfig';
import {
    LarkEmoji,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkUser,
    LarkUserOpenId,
    UserBlacklist,
    ConversationMessage,
    AgentResponse,
} from '@entities';
import { UserGroupBindingRepository as CustomUserGroupBindingRepository } from './user-group-binding-repository';

export const LarkEmojiRepository = AppDataSource.getRepository(LarkEmoji);
export const UserRepository = AppDataSource.getRepository(LarkUser);
export const BaseChatInfoRepository = AppDataSource.getRepository(LarkBaseChatInfo);
export const GroupChatInfoRepository = AppDataSource.getRepository(LarkGroupChatInfo);
export const GroupMemberRepository = AppDataSource.getRepository(LarkGroupMember);
export const LarkUserOpenIdRepository = AppDataSource.getRepository(LarkUserOpenId);

export const UserGroupBindingRepository = new CustomUserGroupBindingRepository(AppDataSource);

export const UserBlacklistRepository = AppDataSource.getRepository(UserBlacklist);
export const ConversationMessageRepository = AppDataSource.getRepository(ConversationMessage);
export const AgentResponseRepository = AppDataSource.getRepository(AgentResponse);
