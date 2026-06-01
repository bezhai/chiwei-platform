// HTTP 飞书入站入口经 getChannelRegistry().get(bot.channel) 取插件 parse/decide。
// 插件靠 import 期自注册，必须确保 @plugins/index 进了 HTTP 服务模块图——否则
// getChannelRegistry().get('lark') fail-closed、每条入站被丢。worker 各自 import
// 了它，HTTP 入站链路由本模块负责拉进来（插件自注册靠 import 副作用触发，
// 这条 side-effect import 不能少）。
import '@plugins/index';
import type {
    LarkReceiveMessage,
    LarkCallbackInfo,
    LarkGroupMemberChangeInfo,
    LarkGroupChangeInfo,
} from 'types/lark';
import { EventHandler } from './event-registry';
import { runRules } from 'core/rules/engine';
import { MessageTransferer } from './factory';
import {
    UpdatePhotoCard,
    FetchPhotoDetails,
    UpdateDailyPhotoCard,
} from 'types/lark';
import { fetchAndSendPhotoDetail } from '@plugins/lark/services/callback/fetch-photo-detail';
import { handleUpdatePhotoCard, handleUpdateDailyPhotoCard } from '@plugins/lark/services/callback/update-card';
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
import { getChannelRegistry } from '@core/registry/channel-registry';
import { searchLarkChatInfo, searchLarkChatMember, addChatMember } from '@lark/basic/group';
import type { LarkEnterChatEvent } from 'types/lark';
import { LarkBaseChatInfo } from 'infrastructure/dal/entities';
import AppDataSource from 'ormconfig';
import { laneRouter } from '@infrastructure/lane-router';
import { context } from '@middleware/context';
import { rabbitmqClient, PROACTIVE_EVAL, CHAT_REQUEST, getLane } from '@integrations/rabbitmq';
import { dispatchInboundIfNeeded } from '@integrations/inbound-lane-dispatch';
import { setNx } from '@cache/redis-client';
import { CommonBotPresence } from 'infrastructure/dal/entities/common-bot-presence';
import { enforceDecision } from '@core/channels/contracts';
import { getBotUnionId } from '@core/services/bot/bot-var';
import { buildLarkRuleMessage } from '@plugins/lark/build-rule-message';
import { larkContextStore } from '@plugins/lark/lark-context-store';
import { setBotMentionTargetResolver } from 'core/rules/rule';
import {
    ensureLarkCommonConversation,
    prepareLarkInboundProjection,
    storeLarkInboundMessage,
} from '@plugins/lark/common-projector';

async function upsertCommonBotPresence(
    commonConversationId: string,
    botName: string | undefined,
    isActive: boolean,
): Promise<void> {
    if (!botName) return;
    await AppDataSource.getRepository(CommonBotPresence).upsert(
        {
            common_conversation_id: commonConversationId,
            bot_name: botName,
            is_active: isActive,
            updated_at: new Date(),
        },
        ['common_conversation_id', 'bot_name'],
    );
}

// runRules 内 NeedRobotMention 谓词的 botMentionTarget 解析（飞书=robot_union_id）。
// 飞书侧 RuleMessage.addressedTargetIds 来源与 hasMention(union_id) 同源
// （见 buildLarkRuleMessage / plugins/lark 的 inbound），口径一致，逐场景行为零变化。
// 非飞书消息（QQ 等）无 lark channelContext，按各自 channel 的 addressing
// 口径——此 resolver 只对 lark 给 robot_union_id，其余给空串（group 不命中、
// direct 仍直通，与各 channel adapter decide 一致）。
setBotMentionTargetResolver((m) => {
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
            // 识图管线仍按飞书裸 message/file id 走飞书自己的管线；bot presence
            // 必须等 common projector 产出 common_conversation_id 后写 common 表，
            // 不能把 oc_* 暴露给 agent-service。

            // ---- 钉死的渠道契约链（决策五 / spec 整体分层，顺序不可调换）----
            // adapter.parse → AddressingPolicy.decide+enforceDecision(前置总闸)
            //   → lark common projector(换 common_*_id)
            //   → runRules(吃平台无关 RuleMessage，单一终态出口)
            //   → storeLarkInboundMessage(写 common+lark) → 发 MQ(makeTextReply 带 common ID)
            // fail-loud（spec 5b）：契约链任一步失败 → 不写库、不发 MQ、记
            // 可查错误日志，**绝不退回飞书裸 ID 往下走**。
            const botName = context.getBotName();
            // 按该 bot 的 channel 经 ChannelRegistry 取对应插件（决策 10：每个
            // bot 用其 channel 对应组件）。bot 配置/未注册插件 = fail-loud，不
            // 静默吞——getChannelRegistry().get() 对未注册 channel 抛错。
            const botConfig = botName ? multiBotManager.getBotConfig(botName) : null;
            let plugin;
            try {
                if (!botConfig) {
                    throw new Error(`bot config not found for "${botName}"`);
                }
                plugin = getChannelRegistry().get(botConfig.channel);
            } catch (resolveErr) {
                console.error(
                    `[inbound] no channel plugin for bot "${botName}" ` +
                        `(channel="${botConfig?.channel}"); fail-loud, message ` +
                        `dropped (not written/queued): ` +
                        `lark_message_id=${message.messageId} ` +
                        `detail=${(resolveErr as Error).message}`,
                );
                return;
            }

            const inbound = plugin.inbound.parse(params);
            if (inbound === null) {
                console.info(
                    `[inbound] adapter parsed null (non-message event), skipped: ` +
                        `lark_message_id=${params.message?.message_id}`,
                );
                return;
            }
            const decision = plugin.addressing.decide(inbound, getBotUnionId());
            enforceDecision(decision, (reason) =>
                console.info(
                    `[inbound] addressing front-gate respond=false: ` +
                        `lark_message_id=${message.messageId} reason=${reason}`,
                ),
            );
            const projection = await prepareLarkInboundProjection(
                params,
                message,
                inbound,
            );
            upsertCommonBotPresence(
                projection.commonConversationId,
                context.getBotName(),
                true,
            ).catch((err) => console.warn('[CommonBotPresence] upsert failed:', err));

            // ---- 处理层泳道分流决策点（lane-routing-redesign §3.1）----
            // 全局 ID 就绪后、派生 RuleMessage / 入库 / 发 MQ 之前：按统一概念
            // （channel + 全局 bot）算 lane。非本进程 lane → 投 inbound_lane.{lane}
            // 给目标 lane 的 channel-server，本地到此为止；本进程 lane → 继续现状链路。
            // flag 默认 off = 完全旁路（dispatchInboundIfNeeded 内零回归），现状行为不变。
            const dispatched = await dispatchInboundIfNeeded({
                currentLane: getLane() ?? 'prod',
                channel: botConfig.channel,
                botGlobalId: botName ?? '',
                eventType: 'im.message.receive_v1',
                globalMessageId: projection.commonMessageId,
                traceId: context.getTraceId(),
                params,
            });
            if (dispatched) {
                // 已投到目标 lane 的 inbound_lane 队列，本进程不再入库/发 MQ
                // （目标 lane channel-server 消费后走入站后半段）。此处尚未
                // buildLarkRuleMessage、还没写 lark store entry，无需 clear。
                return;
            }

            // 全局 ID 就绪。派生平台无关 RuleMessage。飞书强绑能力（admin/群
            // 信息/原始 message_id 等）不再旁挂在 RuleMessage 上：buildLarkRuleMessage
            // 把飞书 Message put 进 lark 私有 store（key=全局 commonMessageId），
            // lark 谓词/handler 按此 key get 取回（B2，#228 的 larkMessage 逃生口已删）。
            const ruleMessage = buildLarkRuleMessage(message, {
                botName: botName ?? '',
                commonUserId: projection.commonUserId,
                commonConversationId: projection.commonConversationId,
                commonMessageId: projection.commonMessageId,
                commonRootMessageId: projection.commonRootMessageId,
                // addressedTargetIds 与 hasMention(union_id) 同源
                // （plugins/lark inbound 的 addressing_hints[].targetId = union_id）。
                addressedTargetIds: inbound.addressing_hints.map((h) => h.targetId),
            });

            // 本条消息处理结束（无论命中哪条退出路径）后，clear lark store entry，
            // 避免 Map 无限增长（内存泄漏）。finally 覆盖 store 失败 return、
            // 抢锁失败 return、publish 成功等所有路径。
            try {
            // ---- 5b 入站重排（决策一/二/三）：顺序硬钉 ----
            //   resolve(契约链, 已完成) → runRules(规则判定 + 各 utility/native
            //   副作用，persona 主链路**不实际 publish**，只把待发 ChatTrigger
            //   意图登记到 RuleTerminalState.pendingChatTrigger)
            //   → storeLarkInboundMessage(无条件执行，不看 terminal kind ——
            //   非 @bot 群消息复读照常入库，飞书逐场景零变化)
            //   → 若 terminal 带 pending 意图：取去重锁；拿到锁才 publish。
            //
            // 为什么 common message 写入必须先于 publish：下游 agent-service
            // chat_node 按 message_id 回查 common_message，读空会短路
            // emit "未找到相关消息记录"。先存后发是硬依赖。
            //
            // runRules 单一终态出口，不向调用方抛错（决策四），所有退出路径
            // 收敛成 RuleTerminalState。blocked / no_match / handler_error
            // 等终态 store 仍无条件执行（与现状一致、行为不变），
            // 只有 pendingChatTrigger 存在才 publish。
            const terminal = await runRules(ruleMessage);

            // storeLarkInboundMessage 写 common_message + lark_message。无条件执行
            // （保住非 @bot 群消息照常入库）。
            // reply_message_id 是飞书"回复某条消息"锚点，与 root 一样由 lark
            // common projector 收敛为 common_message_id 再落库，避免渠道裸 id
            // 与 common_message_id 混用导致回复链断开。
            // 无 parent 时 projection.commonReplyMessageId 为 undefined，保持原"空就空"
            // 语义，不凭空造 id。
            //
            // fail-loud（5b 新增语义）：store 失败 → 记可查错误日志
            // 并 return，**绝不 publish**（否则下游回查读空走"未找到消息
            // 记录"短路，等于发了个注定失败的请求）。
            try {
                await storeLarkInboundMessage(params, projection, message);
            } catch (storeErr) {
                console.error(
                    `[inbound] storeLarkInboundMessage failed (fail-loud, ` +
                        `ChatTrigger NOT published): ` +
                        `message=${projection.commonMessageId} ` +
                        `chat=${projection.commonConversationId} ` +
                        `detail=${(storeErr as Error).message}`,
                );
                return;
            }

            // common/lark message 已成功。若 runRules 登记了待发 ChatTrigger 意图
            // （仅 persona 文本主链路命中时），现在才取去重锁、落 pending
            // 行、发 MQ。三者紧邻（决策二 / 必改2）：多 bot 同群时同一
            // common_message_id 只有第一个拿到锁的 bot 落 common_agent_response pending
            // 行并真正发，其余静默跳过、不写孤儿 pending 行；锁后移避免拿
            // 锁后 store 失败导致锁空占 60s。
            if (terminal.pendingChatTrigger) {
                const { payload, lane, dedupeKey, savePending } =
                    terminal.pendingChatTrigger;
                const lock = await setNx(dedupeKey, '1', 60);
                if (lock === null) {
                    console.info(
                        `[inbound] duplicate ChatTrigger skipped (lock held by ` +
                            `another bot): message=${projection.commonMessageId}`,
                    );
                    return;
                }
                // 抢到锁才落 common_agent_response pending 行（必改2）：未抢锁的
                // bot 已在上面 return，不会到这里 → 不留永不完成的孤儿行。
                await savePending();
                await rabbitmqClient.publish(
                    CHAT_REQUEST,
                    payload as unknown as Record<string, unknown>,
                    undefined,
                    undefined,
                    lane,
                );
                console.info(
                    `[inbound] Published chat.request: ` +
                        `session_id=${payload.session_id}, ` +
                        `message=${projection.commonMessageId}, ` +
                        `lane=${lane || 'prod'}`,
                );
            }
            } finally {
                // 处理结束清理 lark store entry（无内存泄漏）。
                larkContextStore.clear(ruleMessage);
            }
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

        const commonConversationId = await ensureLarkCommonConversation({
            chatId: data.chat_id!,
            scope: 'group',
            displayName: groupInfo.name,
            avatarUrl: groupInfo.avatar,
            memberCount: groupInfo.user_count,
            isActive: !groupInfo.is_leave,
            downloadAllowed: groupInfo.download_has_permission_setting !== 'not_anyone',
        });

        // bot 入群 → 记录 common_bot_presence。agent-service 只读 common 口径。
        const botName = context.getBotName();
        await upsertCommonBotPresence(commonConversationId, botName, true);
    }

    /**
     * 处理机器人被移出群事件
     */
    @EventHandler('im.chat.member.bot.deleted_v1')
    async handleChatRobotRemove(data: LarkGroupMemberChangeInfo): Promise<void> {
        await GroupChatInfoRepository.update(data.chat_id!, {
            is_leave: true,
        });

        // bot 退群 → 标记 common_bot_presence.is_active=false。退群事件只有
        // 飞书 chat_id，先经 lark 层映射回 common_conversation_id。
        const botName = context.getBotName();
        if (botName && data.chat_id) {
            const linkedChat = await AppDataSource.getRepository(LarkBaseChatInfo).findOne({
                where: { chat_id: data.chat_id },
            });
            if (!linkedChat?.common_conversation_id) {
                console.warn(
                    `[CommonBotPresence] cannot mark inactive; no common_conversation_id ` +
                        `for lark chat ${data.chat_id}`,
                );
                return;
            }
            await AppDataSource.getRepository(CommonBotPresence).update(
                {
                    common_conversation_id: linkedChat.common_conversation_id,
                    bot_name: botName,
                },
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
