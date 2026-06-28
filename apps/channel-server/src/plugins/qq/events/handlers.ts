// QQ 入站编排（对飞书 plugins/lark/events/handlers.ts）。顺序与副作用分界与飞书
// 钉死一致，复用 core / 共享通用函数（lane dispatch / runRules / store / publish /
// 去重锁）；QQ 专属只有 custom→InboundMessage（adapter）、qq projector、qq rule message。
//
// 钉死的渠道契约链（顺序不可调换，直面 PR #228 副作用前移翻车）：
//   adapter.parse → AddressingPolicy.decide + enforceDecision(仅记 skip 原因，
//     与飞书一致不早退，非 @bot 群消息照常入库、由 runRules 的 NeedRobotMention gate)
//   → qq projector(换 common_*_id)
//   → lane dispatch(非本进程 lane 投 inbound_lane.{lane}，本地到此为止)
//   ──── 分界：以下副作用仅在实际处理 lane 执行 ────
//   → presence → 识图 → buildQqRuleMessage → runRules
//   → storeQqInboundMessage(无条件，fail-loud：失败不 publish)
//   → pendingChatTrigger 去重锁 → publish CHAT_REQUEST
import '@plugins/index';

import type { CustomInboundMessage } from '@inner/shared/protocols';
import AppDataSource from 'ormconfig';
import { context } from '@middleware/context';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { getChannelRegistry } from '@core/registry/channel-registry';
import { enforceDecision } from '@core/channels/contracts';
import { runRules } from '@core/rules/engine';
import { rabbitmqClient, CHAT_REQUEST, getLane } from '@integrations/rabbitmq';
import { dispatchInboundIfNeeded } from '@integrations/inbound-lane-dispatch';
import { setNx } from '@cache/redis-client';
import { CommonBotPresence } from '@entities/common-bot-presence';
import { QQ_SELF_MENTION_TARGET } from '../inbound';
import { buildQqRuleMessage } from '../build-rule-message';
import { enqueueQqImagePipeline } from '../image-pipeline';
import {
    claimQqInboundMessageForBot,
    prepareQqInboundProjection,
    storeQqInboundMessage,
    withQqInboundProjectionLock,
} from '../common-projector';

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

export class QqEventHandlers {
    // 入参是网关已归一化的 CustomInboundMessage；context.botName 由 ingress / lane
    // consumer 在调用前注入（HTTP ingress 用 payload.botName，lane envelope 用信封 bot_name）。
    async handleInbound(custom: CustomInboundMessage): Promise<void> {
        try {
            const botName = context.getBotName();
            const botConfig = botName ? multiBotManager.getBotConfig(botName) : null;
            let plugin;
            let botCommonUserId: string;
            try {
                if (!botConfig) {
                    throw new Error(`bot config not found for "${botName}"`);
                }
                const id = botConfig.common_user_id;
                if (!id) {
                    throw new Error(
                        `bot "${botName}" has no common_user_id; bot identity ` +
                            'initialization must run before inbound handling',
                    );
                }
                botCommonUserId = id;
                plugin = getChannelRegistry().get(botConfig.channel);
            } catch (resolveErr) {
                console.error(
                    `[qq inbound] no channel plugin for bot "${botName}" ` +
                        `(channel="${botConfig?.channel}"); fail-loud, message dropped ` +
                        `(not written/queued): qq_message_id=${custom?.messageId} ` +
                        `detail=${(resolveErr as Error).message}`,
                );
                return;
            }

            const inbound = plugin.inbound.parse(custom);
            if (inbound === null) {
                console.info(
                    `[qq inbound] adapter parsed null (non-message payload), skipped: ` +
                        `qq_message_id=${custom?.messageId}`,
                );
                return;
            }

            const decision = plugin.addressing.decide(inbound, QQ_SELF_MENTION_TARGET);
            // 与飞书一致：front-gate 只记 skip 原因、不早退。非 @bot 群消息照常入库，
            // 真正的回复 gate 在 runRules 的 NeedRobotMention。
            enforceDecision(decision, (reason) =>
                console.info(
                    `[qq inbound] addressing front-gate respond=false: ` +
                        `qq_message_id=${custom.messageId} reason=${reason}`,
                ),
            );

            await withQqInboundProjectionLock(custom.messageId, async () => {
                const projection = await prepareQqInboundProjection(
                    inbound,
                    botName ?? '',
                    botCommonUserId,
                );

                const dispatched = await dispatchInboundIfNeeded({
                    currentLane: getLane() ?? 'prod',
                    channel: botConfig.channel,
                    botGlobalId: botName ?? '',
                    commonConversationId: projection.commonConversationId,
                    eventType: 'qq.message.receive',
                    globalMessageId: projection.commonMessageId,
                    traceId: context.getTraceId(),
                    params: custom,
                });
                if (dispatched) return;

                // ---- 分界后：仅本 lane 执行的副作用 ----
                upsertCommonBotPresence(
                    projection.commonConversationId,
                    botName,
                    true,
                ).catch((err) => console.warn('[qq CommonBotPresence] upsert failed:', err));

                enqueueQqImagePipeline(inbound, projection.commonMessageId, botName);

                const ruleMessage = buildQqRuleMessage(inbound, {
                    botName: botName ?? '',
                    commonUserId: projection.commonUserId,
                    commonConversationId: projection.commonConversationId,
                    commonMessageId: projection.commonMessageId,
                    commonRootMessageId: projection.commonRootMessageId,
                    botCommonUserId,
                    mentionedUserIds: projection.mentionedUserIds,
                });

                const terminal = await runRules(ruleMessage);

                try {
                    await storeQqInboundMessage(
                        inbound,
                        projection,
                        custom as unknown as Record<string, unknown>,
                    );
                } catch (storeErr) {
                    console.error(
                        `[qq inbound] storeQqInboundMessage failed (fail-loud, ` +
                            `ChatTrigger NOT published): message=${projection.commonMessageId} ` +
                            `chat=${projection.commonConversationId} ` +
                            `detail=${(storeErr as Error).message}`,
                    );
                    return;
                }

                if (terminal.pendingChatTrigger) {
                    const { payload, lane, dedupeKey, savePending } = terminal.pendingChatTrigger;
                    const lock = await setNx(dedupeKey, '1', 60);
                    if (lock === null) {
                        console.info(
                            `[qq inbound] duplicate ChatTrigger skipped (lock held): ` +
                                `message=${projection.commonMessageId}`,
                        );
                        return;
                    }
                    if (!botName) {
                        throw new Error(
                            `cannot claim common message ${projection.commonMessageId}: ` +
                                'botName missing from context',
                        );
                    }
                    await claimQqInboundMessageForBot({
                        commonMessageId: projection.commonMessageId,
                        botName,
                        commonUserId: projection.commonUserId,
                    });
                    await savePending();
                    await rabbitmqClient.publish(
                        CHAT_REQUEST,
                        payload as unknown as Record<string, unknown>,
                        undefined,
                        undefined,
                        lane,
                    );
                    console.info(
                        `[qq inbound] Published chat.request: session_id=${payload.session_id}, ` +
                            `message=${projection.commonMessageId}, lane=${lane || 'prod'}`,
                    );
                }
            });
        } catch (error) {
            console.error(
                'Error handling qq inbound:',
                (error as Error).message,
                (error as Error).stack,
            );
        }
    }
}

export const qqEventHandlers = new QqEventHandlers();
