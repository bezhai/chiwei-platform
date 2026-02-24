import {
    LarkReceiveMessage,
    LarkCallbackInfo,
    LarkGroupMemberChangeInfo,
    LarkGroupChangeInfo,
} from 'types/lark';
import { EventHandler } from './event-registry';
import { runRules } from 'core/rules/engine';
import { MessageTransferer } from './factory';
import { storeMessage } from 'infrastructure/integrations/memory';
import {
    UpdatePhotoCard,
    FetchPhotoDetails,
    UpdateDailyPhotoCard,
    LarkCardRetry,
    LarkCardThumbsDown,
    LarkCardThumbsUp,
} from 'types/lark';
import { fetchAndSendPhotoDetail } from '@core/services/callback/fetch-photo-detail';
import { handleUpdatePhotoCard, handleUpdateDailyPhotoCard } from '@core/services/callback/update-card';
import { handleRetryCard } from '@core/services/callback/retry-card';
import { handleFeedback } from '@core/services/callback/feedback';
import { LarkGroupMember, LarkUser } from 'infrastructure/dal/entities';
import { LarkUserOpenId } from 'infrastructure/dal/entities/lark-user-open-id';
import { getUserInfo } from 'infrastructure/integrations/lark-client';
import {
    GroupMemberRepository,
    UserRepository,
    LarkUserOpenIdRepository,
    GroupChatInfoRepository,
    UserGroupBindingRepository,
} from 'infrastructure/dal/repositories/repositories';
import { getBotAppId } from '@core/services/bot/bot-var';
import { searchLarkChatInfo, searchLarkChatMember, addChatMember } from '@lark/basic/group';
import { LarkEnterChatEvent } from 'types/lark';
import { LarkBaseChatInfo } from 'infrastructure/dal/entities';
import AppDataSource from 'ormconfig';
import { ImageProcessorService } from '@core/services/media/image-processor';

/**
 * Lark事件处理器类
 * 使用装饰器自动注册事件处理器
 */
export class LarkEventHandlers {
    /**
     * 处理消息接收事件
     */
    @EventHandler('im.message.receive_v1')
    async handleMessageReceive(params: LarkReceiveMessage): Promise<void> {
        try {
            const message = await MessageTransferer.transfer(params);
            if (!message) {
                console.warn(
                    'Failed to build message, skipping:',
                    params.message.message_id,
                    params.message.message_type,
                );
                return;
            }

            if (message.allowDownloadResource()) {
                const uploadPhotoService = ImageProcessorService.getInstance();
                for (const imageKey of message.imageKeys()) {
                    uploadPhotoService
                        .processImage({
                            message_id: message.messageId,
                            file_key: imageKey,
                        })
                        .catch((err) => {
                            console.error('Error in upload image:', err);
                        });
                }
            }

            await storeMessage({
                user_id: message.sender,
                content: message.toStorageFormat(),
                role: 'user',
                message_id: message.messageId,
                chat_id: message.chatId,
                chat_type: message.isP2P() ? 'p2p' : 'group',
                create_time: message.createTime ?? '0',
                root_message_id: message.rootId,
                reply_message_id: message.parentMessageId,
                message_type: message.messageType,
            });

            await runRules(message);
        } catch (error) {
            console.error(
                'Error handling message receive:',
                (error as Error).message,
                (error as Error).stack,
            );
        }
    }

    /**
     * 处理消息撤回事件
     */
    @EventHandler('im.message.recalled_v1')
    async handleMessageRecalled(): Promise<void> {
        // pass 占位
    }

    /**
     * 处理卡片动作事件
     */
    @EventHandler('card.action.trigger')
    async handleCardAction(data: LarkCallbackInfo): Promise<void> {
        switch (data.action.value?.type) {
            case UpdatePhotoCard:
                handleUpdatePhotoCard(data, data.action.value.tags);
                break;
            case FetchPhotoDetails:
                fetchAndSendPhotoDetail(data, data.action.value.images);
                break;
            case UpdateDailyPhotoCard:
                handleUpdateDailyPhotoCard(data, data.action.value.start_time);
                break;
            case LarkCardRetry:
                handleRetryCard(data.action.value);
                break;
            case LarkCardThumbsDown:
                handleFeedback(data.action.value, data.operator.union_id);
                break;
            case LarkCardThumbsUp:
                handleFeedback(data.action.value, data.operator.union_id);
                break;
            default:
                console.warn('unknown card action', data);
        }
    }

    /**
     * 处理群成员添加事件
     */
    @EventHandler('im.chat.member.user.added_v1')
    async handleChatMemberAdd(data: LarkGroupMemberChangeInfo): Promise<void> {
        const updateUsers: LarkGroupMember[] =
            data.users?.map((user) => {
                return {
                    union_id: user.user_id?.union_id!,
                    chat_id: data.chat_id!,
                    is_leave: false,
                    created_at: new Date(),
                    updated_at: new Date(),
                };
            }) || [];
        const users: LarkUser[] =
            data.users?.map((user) => {
                return {
                    union_id: user.user_id?.union_id!,
                    name: user.name!,
                };
            }) || [];
        const openIds: LarkUserOpenId[] =
            data.users?.map((user) => {
                return {
                    appId: getBotAppId(),
                    openId: user.user_id?.open_id!,
                    unionId: user.user_id?.union_id!,
                    name: user.name!,
                };
            }) || [];

        await Promise.all([
            GroupMemberRepository.save(updateUsers),
            UserRepository.save(users),
            LarkUserOpenIdRepository.save(openIds),
            GroupChatInfoRepository.increment({ chat_id: data.chat_id! }, 'user_count', 1),
        ]);
    }

    /**
     * 处理群成员移除事件
     */
    @EventHandler(['im.chat.member.user.deleted_v1', 'im.chat.member.user.withdrawn_v1'])
    async handleChatMemberRemove(data: LarkGroupMemberChangeInfo): Promise<void> {
        const updateUsers: LarkGroupMember[] =
            data.users?.map((user) => {
                return {
                    union_id: user.user_id?.union_id!,
                    chat_id: data.chat_id!,
                    is_leave: true,
                    updated_at: new Date(),
                };
            }) || [];

        await Promise.all([
            GroupMemberRepository.save(updateUsers),
            GroupChatInfoRepository.increment({ chat_id: data.chat_id! }, 'user_count', -1),
        ]);

        // 检查是否有绑定关系，如果有则重新拉入群
        for (const user of data.users || []) {
            const binding = await UserGroupBindingRepository.findByUserAndChat(
                user.user_id?.union_id!,
                data.chat_id!,
            );
            if (binding && binding.isActive) {
                // 重新拉入群
                await Promise.all([
                    addChatMember(data.chat_id!, user.user_id?.open_id!),
                    GroupChatInfoRepository.increment({ chat_id: data.chat_id! }, 'user_count', 1),
                ]);
            }
        }
    }

    /**
     * 处理机器人被添加到群事件
     */
    @EventHandler('im.chat.member.bot.added_v1')
    async handleChatRobotAdd(data: LarkGroupMemberChangeInfo): Promise<void> {
        console.info(`upsert chat ${data.chat_id}`);
        const { groupInfo, members } = await searchLarkChatInfo(data.chat_id!);
        await Promise.all([
            GroupMemberRepository.save(members),
            GroupChatInfoRepository.save(groupInfo),
        ]);
        const {
            users,
            members: newMembers,
            openIdUsers,
        } = await searchLarkChatMember(data.chat_id!);
        await Promise.all([
            GroupMemberRepository.save(newMembers),
            UserRepository.save(users),
            LarkUserOpenIdRepository.save(openIdUsers),
        ]);
    }

    /**
     * 处理机器人被移出群事件
     */
    @EventHandler('im.chat.member.bot.deleted_v1')
    async handleChatRobotRemove(data: LarkGroupMemberChangeInfo): Promise<void> {
        await GroupChatInfoRepository.update(data.chat_id!, {
            is_leave: true,
        });
    }

    /**
     * 处理消息反应事件
     */
    @EventHandler(['im.message.reaction.created_v1', 'im.message.reaction.deleted_v1'])
    async handleReaction(): Promise<void> {
        // pass 占位
    }

    /**
     * 处理进入聊天事件
     */
    @EventHandler('im.chat.access_event.bot_p2p_chat_entered_v1')
    async handlerEnterChat(data: LarkEnterChatEvent): Promise<void> {
        await AppDataSource.transaction(async (manager) => {
            const baseChatInfoRepository = manager.getRepository(LarkBaseChatInfo);
            const groupMemberRepository = manager.getRepository(LarkGroupMember);
            const userRepository = manager.getRepository(LarkUser);

            const unionId = data.operator_id!.union_id!;

            await Promise.allSettled([
                (async () => {
                    // 查询是否已经存在, 不存在则创建
                    const baseChatInfo = await baseChatInfoRepository.findOne({
                        where: { chat_id: data.chat_id },
                    });
                    if (!baseChatInfo) {
                        // 1. 创建基础聊天信息
                        await baseChatInfoRepository.save({
                            chat_id: data.chat_id!,
                            chat_mode: 'p2p',
                        });
                    }

                    // 2. 创建用户与聊天的关联关系（使用 lark_group_member 表）
                    await groupMemberRepository.save({
                        chat_id: data.chat_id!,
                        union_id: unionId,
                        is_owner: false,
                        is_manager: false,
                        is_leave: false,
                    });
                })(),
                // 3. 检查并创建用户信息
                (async () => {
                    const existingUser = await userRepository.findOne({
                        where: { union_id: unionId },
                    });

                    if (!existingUser) {
                        try {
                            const userInfo = await getUserInfo(unionId);

                            const newUser = new LarkUser();
                            newUser.union_id = unionId;
                            newUser.name = userInfo.user?.name || '未知用户';
                            newUser.avatar_origin = userInfo.user?.avatar?.avatar_origin;

                            await userRepository.save(newUser);

                            console.info(`Created new user record for union_id: ${unionId}`);
                        } catch (error) {
                            console.error(
                                `Failed to fetch user info for union_id ${unionId}:`,
                                error,
                            );
                        }
                    }
                })(),
            ]);
        });
    }

    /**
     * 处理群信息变更事件
     */
    @EventHandler('im.chat.updated_v1')
    async handleGroupChange(data: LarkGroupChangeInfo): Promise<void> {
        console.info(`upsert chat ${data.chat_id}`);
        const { groupInfo } = await searchLarkChatInfo(data.chat_id!);
        await GroupChatInfoRepository.save(groupInfo);
    }
}

// 创建单例实例并导出
export const larkEventHandlers = new LarkEventHandlers();
