import type {
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
} from 'types/lark';
import { fetchAndSendPhotoDetail } from '@core/services/callback/fetch-photo-detail';
import { handleUpdatePhotoCard, handleUpdateDailyPhotoCard } from '@core/services/callback/update-card';
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
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { searchLarkChatInfo, searchLarkChatMember, addChatMember } from '@lark/basic/group';
import type { LarkEnterChatEvent } from 'types/lark';
import { LarkBaseChatInfo } from 'infrastructure/dal/entities';
import AppDataSource from 'ormconfig';
import { laneRouter } from '@infrastructure/lane-router';
import { context } from '@middleware/context';
import { rabbitmqClient, PROACTIVE_EVAL } from '@integrations/rabbitmq';
import { BotChatPresence } from 'infrastructure/dal/entities/bot-chat-presence';
import { runInboundContractChain } from '@core/channels/inbound-pipeline';
import { getIdentityResolver } from '@integrations/identity-resolver-runtime';
import { getBotUnionId } from '@core/services/bot/bot-var';
import { buildLarkRuleMessage } from 'core/rules/rule-message';
import { setBotIdentityResolver } from 'core/rules/rule';

// runRules 内 NeedRobotMention 谓词的 botIdentity 解析（飞书=robot_union_id）。
// 飞书侧 RuleMessage.addressedTargetIds 来源与 hasMention(union_id) 同源
// （见 buildLarkRuleMessage / lark-adapter），口径一致，逐场景行为零变化。
// 非飞书消息（QQ 等）无 lark channelContext，按各自 channel 的 addressing
// 口径——此 resolver 只对 lark 给 robot_union_id，其余给空串（group 不命中、
// direct 仍直通，与各 channel adapter decide 一致）。
setBotIdentityResolver((m) => {
    if (m.channel !== 'lark') return '';
    // robot_union_id 在 bot_config.credentials（T4）；getBotUnionId 读当前
    // context bot，与现状 NeedRobotMention 同源。无 context 时给空串
    // （group 不命中、direct 仍直通，与现状一致）。
    try {
        return getBotUnionId();
    } catch {
        return '';
    }
});

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
                const toolClient = laneRouter.createClient('tool-service');
                const botName = context.getBotName();
                for (const imageKey of message.imageKeys()) {
                    toolClient.post(
                        '/api/image-pipeline/process',
                        { message_id: message.messageId, file_key: imageKey },
                        {
                            headers: {
                                Authorization: `Bearer ${process.env.INNER_HTTP_SECRET}`,
                                'X-App-Name': botName,
                            },
                        },
                    ).catch((err) => {
                        console.error('Error in upload image:', err);
                    });
                }
            }

            // ---- 飞书 native 渠道专属副作用（保留不动，spec G）----
            // 识图管线 / bot_chat_presence 是飞书渠道专属（QQ 不需要），不在
            // chatRules 里、按飞书裸 ID 走飞书自己的管线，与身份契约链解耦。
            // 它们对 contract chain 成败无依赖，先做（保留现状行为零变化）。
            const currentBotName = context.getBotName();
            if (currentBotName && message.chatId) {
                AppDataSource.getRepository(BotChatPresence)
                    .upsert(
                        { chat_id: message.chatId, bot_name: currentBotName, is_active: true, updated_at: new Date() },
                        ['chat_id', 'bot_name'],
                    )
                    .catch(err => console.warn('[BotChatPresence] upsert failed:', err));
            }

            // ---- 钉死的渠道契约链（决策五 / spec 整体分层，顺序不可调换）----
            // adapter.parse → AddressingPolicy.decide+enforceDecision(前置总闸)
            //   → IdentityResolver.resolve(换全局 internal_*_id)
            //   → runRules(吃平台无关 RuleMessage，单一终态出口)
            //   → storeMessage(写全局 ID) → 发 MQ(makeTextReply 带 channel+全局 ID)
            // fail-loud（spec 5b）：契约链任一步失败 → 不写库、不发 MQ、记
            // 可查错误日志，**绝不退回飞书裸 ID 往下走**。
            const botName = context.getBotName();
            const triple = botName ? multiBotManager.getChannelTriple(botName) : null;
            if (!triple) {
                // 装配不出三件套 = bot 配置/加载异常 = fail-loud（不静默吞）。
                console.error(
                    `[inbound] no channel triple for bot "${botName}"; ` +
                        `fail-loud, message dropped (not written/queued): ` +
                        `lark_message_id=${message.messageId}`,
                );
                return;
            }

            const chain = await runInboundContractChain({
                params,
                parse: (raw) => triple.inbound.parse(raw),
                decide: (m, b) => triple.addressing.decide(m, b),
                // 飞书 botIdentity 口径 = robot_union_id，与现状
                // NeedRobotMention / LarkAddressingPolicy 同源。
                botIdentity: getBotUnionId(),
                resolver: getIdentityResolver(),
                logSkip: (reason) =>
                    console.info(
                        `[inbound] addressing front-gate respond=false: ` +
                            `lark_message_id=${message.messageId} reason=${reason}`,
                    ),
            });

            if (!chain.ok) {
                if (chain.reason === 'parsed_null') {
                    // adapter 判定这不是要处理的消息（飞书杂事件）。
                    console.info(
                        `[inbound] adapter parsed null (non-message event), skipped: ` +
                            `lark_message_id=${params.message?.message_id}`,
                    );
                    return;
                }
                // contract_chain_error：fail-loud —— 不写库、不发 MQ、不退裸 ID。
                console.error(
                    `[inbound] contract chain failed (fail-loud, message NOT ` +
                        `stored/queued, no raw-id fallback): ` +
                        `lark_message_id=${message.messageId} detail=${chain.detail}`,
                );
                return;
            }

            // 全局 ID 就绪。派生平台无关 RuleMessage（飞书强绑能力经
            // channelContext 旁挂 Message，lark-only handler 用 requireLarkContext
            // 取回跑不变内部逻辑）。
            const ruleMessage = buildLarkRuleMessage(message, {
                botName: botName ?? '',
                internalUserId: chain.globalUserId,
                internalChatId: chain.globalChatId,
                internalMessageId: chain.globalMessageId,
                internalRootId: chain.globalRootId,
                // addressedTargetIds 与 hasMention(union_id) 同源
                // （lark-adapter 的 addressing_hints[].targetId = union_id）。
                addressedTargetIds: chain.inbound.addressing_hints.map(
                    (h) => h.targetId,
                ),
            });

            // 识图管线（飞书渠道专属，spec G 保留）：按飞书裸 message_id /
            // image_key 走飞书 native 管线，与全局身份契约链解耦。
            if (message.allowDownloadResource()) {
                const toolClient = laneRouter.createClient('tool-service');
                for (const imageKey of message.imageKeys()) {
                    toolClient.post(
                        '/api/image-pipeline/process',
                        { message_id: message.messageId, file_key: imageKey },
                        {
                            headers: {
                                Authorization: `Bearer ${process.env.INNER_HTTP_SECRET}`,
                                'X-App-Name': botName,
                            },
                        },
                    ).catch((err) => {
                        console.error('Error in upload image:', err);
                    });
                }
            }

            // storeMessage 写全局 internal_*_id（决策二/spec D）。
            // reply_message_id 是飞书"回复某条消息"锚点，5c 读取方/快照范围，
            // 本步保持原值不动，不静默猜。runRules 的 persona 主链路
            // makeTextReply 已直接消费 RuleMessage 上的全局 ID（reply.ts）。
            await storeMessage({
                user_id: chain.globalUserId,
                content: message.toStorageFormat(),
                role: 'user',
                message_id: chain.globalMessageId,
                chat_id: chain.globalChatId,
                chat_type: message.isP2P() ? 'p2p' : 'group',
                create_time: message.createTime ?? '0',
                root_message_id: chain.globalRootId ?? chain.globalMessageId,
                reply_message_id: message.parentMessageId,
                message_type: message.messageType,
            });

            // runRules：平台无关统一引擎，单一终态出口。前置总闸 respond
            // 仅 gate persona 主链路；飞书 native 复读用 NeedNotRobotMention，
            // 非 @bot 群消息必须照常进 runRules（否则飞书复读回归——违反
            // "飞书逐场景零变化"硬约束）。故无论 respond 与否都进 runRules，
            // persona 链路由 NeedRobotMention 谓词自行 gate（与现状等价）。
            await runRules(ruleMessage);
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

        // bot 入群 → 记录 bot_chat_presence
        const botName = context.getBotName();
        if (botName && data.chat_id) {
            await AppDataSource.getRepository(BotChatPresence)
                .upsert(
                    { chat_id: data.chat_id, bot_name: botName, is_active: true, updated_at: new Date() },
                    ['chat_id', 'bot_name'],
                );
        }
    }

    /**
     * 处理机器人被移出群事件
     */
    @EventHandler('im.chat.member.bot.deleted_v1')
    async handleChatRobotRemove(data: LarkGroupMemberChangeInfo): Promise<void> {
        await GroupChatInfoRepository.update(data.chat_id!, {
            is_leave: true,
        });

        // bot 退群 → 标记 is_active=false
        const botName = context.getBotName();
        if (botName && data.chat_id) {
            await AppDataSource.getRepository(BotChatPresence)
                .update(
                    { chat_id: data.chat_id, bot_name: botName },
                    { is_active: false, updated_at: new Date() },
                );
        }
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
