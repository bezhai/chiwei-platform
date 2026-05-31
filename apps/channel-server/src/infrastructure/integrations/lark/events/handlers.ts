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
import { storeMessage } from 'infrastructure/integrations/memory';
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
import { BotChatPresence } from 'infrastructure/dal/entities/bot-chat-presence';
import { runInboundContractChain } from '@core/channels/inbound-pipeline';
import { getIdentityResolver } from '@integrations/identity-resolver-runtime';
import { getBotUnionId } from '@core/services/bot/bot-var';
import { buildLarkRuleMessage } from '@plugins/lark/build-rule-message';
import { larkContextStore } from '@plugins/lark/lark-context-store';
import { setBotIdentityResolver } from 'core/rules/rule';

// runRules 内 NeedRobotMention 谓词的 botIdentity 解析（飞书=robot_union_id）。
// 飞书侧 RuleMessage.addressedTargetIds 来源与 hasMention(union_id) 同源
// （见 buildLarkRuleMessage / plugins/lark 的 inbound），口径一致，逐场景行为零变化。
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

            const chain = await runInboundContractChain({
                params,
                parse: (raw) => plugin.inbound.parse(raw),
                decide: (m, b) => plugin.addressing.decide(m, b),
                // 飞书 botIdentity 口径 = robot_union_id，与现状
                // NeedRobotMention / plugins/lark addressing 同源。
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
                globalMessageId: chain.globalMessageId,
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
            // 把飞书 Message put 进 lark 私有 store（key=全局 internalMessageId），
            // lark 谓词/handler 按此 key get 取回（B2，#228 的 larkMessage 逃生口已删）。
            const ruleMessage = buildLarkRuleMessage(message, {
                botName: botName ?? '',
                internalUserId: chain.globalUserId,
                internalChatId: chain.globalChatId,
                internalMessageId: chain.globalMessageId,
                internalRootId: chain.globalRootId,
                // addressedTargetIds 与 hasMention(union_id) 同源
                // （plugins/lark inbound 的 addressing_hints[].targetId = union_id）。
                addressedTargetIds: chain.inbound.addressing_hints.map(
                    (h) => h.targetId,
                ),
            });

            // 本条消息处理结束（无论命中哪条退出路径）后，clear lark store entry，
            // 避免 Map 无限增长（内存泄漏）。finally 覆盖 storeMessage 失败 return、
            // 抢锁失败 return、publish 成功等所有路径。
            try {
            // ---- 5b 入站重排（决策一/二/三）：顺序硬钉 ----
            //   resolve(契约链, 已完成) → runRules(规则判定 + 各 utility/native
            //   副作用，persona 主链路**不实际 publish**，只把待发 ChatTrigger
            //   意图登记到 RuleTerminalState.pendingChatTrigger)
            //   → storeMessage(无条件执行，不看 terminal kind ——
            //   非 @bot 群消息复读照常入库，飞书逐场景零变化)
            //   → 若 terminal 带 pending 意图：取去重锁；拿到锁才 publish。
            //
            // 为什么 storeMessage 必须先于 publish：下游 agent-service
            // chat_node 按 message_id 回查 conversation_messages，读空会短路
            // emit "未找到相关消息记录"。先存后发是硬依赖。
            //
            // runRules 单一终态出口，不向调用方抛错（决策四），所有退出路径
            // 收敛成 RuleTerminalState。blocked / no_match / handler_error
            // 等终态 storeMessage 仍无条件执行（与现状一致、行为不变），
            // 只有 pendingChatTrigger 存在才 publish。
            const terminal = await runRules(ruleMessage);

            // storeMessage 写全局 internal_*_id（决策二/spec D）。无条件执行
            // （保住非 @bot 群消息照常入库）。
            // reply_message_id 是飞书"回复某条消息"锚点，与 root 一样经
            // IdentityResolver 翻成全局 internal_message_id 再落库 —— 否则
            // 裸 parentMessageId 与全局 message_id 主键失配，按它做回复链
            // walk 的读取方（cross_chat.py / _context_messages.py）会断链。
            // 无 parent 时 chain.globalReplyToId 为 undefined，保持原"空就空"
            // 语义，不凭空造 id。
            //
            // fail-loud（5b 新增语义）：storeMessage 失败 → 记可查错误日志
            // 并 return，**绝不 publish**（否则下游回查读空走"未找到消息
            // 记录"短路，等于发了个注定失败的请求）。
            try {
                await storeMessage({
                    user_id: chain.globalUserId,
                    // 发送者显示名冗余落库。来源 = message.senderInfo
                    // （MessageBuilder.buildMetadataFromEvent 已按 union_id
                    // 拉过的 LarkUser 行），不新造数据源。拉不到则留空，
                    // 读取端按空处理（决策：不写脏占位）。
                    username: message.senderInfo?.name,
                    content: message.toStorageFormat(),
                    role: 'user',
                    message_id: chain.globalMessageId,
                    chat_id: chain.globalChatId,
                    chat_type: message.isP2P() ? 'p2p' : 'group',
                    create_time: message.createTime ?? '0',
                    root_message_id: chain.globalRootId ?? chain.globalMessageId,
                    reply_message_id: chain.globalReplyToId,
                    message_type: message.messageType,
                });
            } catch (storeErr) {
                console.error(
                    `[inbound] storeMessage failed (fail-loud, ChatTrigger ` +
                        `NOT published): message=${chain.globalMessageId} ` +
                        `chat=${chain.globalChatId} ` +
                        `detail=${(storeErr as Error).message}`,
                );
                return;
            }

            // storeMessage 已成功。若 runRules 登记了待发 ChatTrigger 意图
            // （仅 persona 文本主链路命中时），现在才取去重锁、落 pending
            // 行、发 MQ。三者紧邻（决策二 / 必改2）：多 bot 同群时同一全局
            // message_id 只有第一个拿到锁的 bot 落 agent_responses pending
            // 行并真正发，其余静默跳过、不写孤儿 pending 行；锁后移避免拿
            // 锁后 storeMessage 失败导致锁空占 60s。
            if (terminal.pendingChatTrigger) {
                const { payload, lane, dedupeKey, savePending } =
                    terminal.pendingChatTrigger;
                const lock = await setNx(dedupeKey, '1', 60);
                if (lock === null) {
                    console.info(
                        `[inbound] duplicate ChatTrigger skipped (lock held by ` +
                            `another bot): message=${chain.globalMessageId}`,
                    );
                    return;
                }
                // 抢到锁才落 agent_responses pending 行（必改2）：未抢锁的
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
                        `message=${chain.globalMessageId}, ` +
                        `lane=${lane || 'prod'}`,
                );
            }
            } finally {
                // 处理结束清理 lark store entry（无内存泄漏）。
                larkContextStore.clear(ruleMessage.internalMessageId);
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
