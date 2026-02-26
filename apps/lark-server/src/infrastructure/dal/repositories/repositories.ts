import AppDataSource from 'ormconfig';
import {
    LarkEmoji,
    LarkBaseChatInfo,
    LarkGroupChatInfo,
    LarkGroupMember,
    LarkUser,
    LarkCardContext,
    LarkUserOpenId,
    ResponseFeedback,
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
export const CardContextRepository = AppDataSource.getRepository(LarkCardContext);
export const UserGroupBindingRepository = new CustomUserGroupBindingRepository(AppDataSource);
export const ResponseFeedbackRepository = AppDataSource.getRepository(ResponseFeedback);
export const UserBlacklistRepository = AppDataSource.getRepository(UserBlacklist);
export const ConversationMessageRepository = AppDataSource.getRepository(ConversationMessage);
export const AgentResponseRepository = AppDataSource.getRepository(AgentResponse);
