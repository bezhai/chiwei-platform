// QQ 入站编排。复用 @inner/shared 的通用函数（lane dispatch / runRules / store）；
// QQ 专属只有 custom→InboundMessage（adapter）、qq projector、qq rule message。
//
// 顺序与副作用分界当初是照着飞书那条链定的。飞书拆走之后它自己的编排在
// apps/lark-service/src/lark/receive-message.ts，两边不再共用代码，也不再要求一致
//（那个文件开头写了它为什么故意改了顺序）。
//
// **入站到落库为止。** 「这条消息触发一次聊天请求」这个概念已经不存在了：赤尾不从队列
// 拿消息，她每一缝直接查 common_message、自己决定要不要开口（见 agent-service 的
// app/living）。所以这里既不发 MQ、也不抢去重锁、也不认领消息、也不落
// common_agent_response 的 pending 行。runRules 留着是因为指令仍然走它 —— QQ 今天一条
// 平台指令都没注册（plugins/qq/index.ts 的 commands=[]），所以它的产出只有一条终态日志。
//
// 钉死的渠道契约链（顺序不可调换，直面 PR #228 副作用前移翻车）：
//   adapter.parse → AddressingPolicy.decide + enforceDecision(仅记 skip 原因、
//     不早退，非 @bot 群消息照常入库、由 runRules 的 NeedRobotMention gate)
//   → qq projector(换 common_*_id)
//   → lane 判定(非本进程 lane：备好信封，本地到此为止，投递在投影锁外做)
//   ──── 分界：以下副作用仅在实际处理 lane 执行 ────
//   → presence → 识图 → buildQqRuleMessage → runRules
//   → storeQqInboundMessage(无条件，失败上抛：不让调用方回 2xx)
import '@plugins/index';

import type { CustomInboundMessage } from '@inner/shared/protocols';
import AppDataSource from 'ormconfig';
import { context } from '@middleware/context';
import { botDirectory } from '@inner/shared/bot';
import { getChannelRegistry } from '@inner/shared/channel';
import { enforceDecision } from '@inner/shared/channel';
import { runRules } from '@inner/shared/rules';
import { getLane } from '@inner/shared/mq';
import { handOffToLane, resolveInboundLaneHandoff } from '@integrations/lane-handoff';
import { CommonBotPresence } from '@inner/shared/entities';
import { QQ_SELF_MENTION_TARGET } from '../inbound';
import { buildQqRuleMessage } from '../build-rule-message';
import { enqueueQqImagePipeline } from '../image-pipeline';
import {
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

export interface QqInboundOptions {
    /**
     * 这条消息是 prod 交接过来的（走 /api/internal/qq/lane-inbound）。交接过的消息
     * 不再做泳道判定：sidecar 在目标泳道的 Service 不存在时会把请求打回 prod 自己，
     * 再判一次泳道就会得到同一个目标、再交接一次，无限自投。
     */
    handedOff?: boolean;
}

export class QqEventHandlers {
    // 入参是网关已归一化的 CustomInboundMessage；context.botName 由 ingress / 信封端点
    // 在调用前注入（HTTP ingress 用 payload.botName，泳道信封用信封的 bot_name）。
    //
    // 失败一律往上抛：调用方要据此决定 HTTP 状态码，投递方只有非 2xx 才知道这条消息
    // 没人处理。
    async handleInbound(
        custom: CustomInboundMessage,
        options: QqInboundOptions = {},
    ): Promise<void> {
        const botName = context.getBotName();
        const botConfig = botName ? botDirectory.getBotConfig(botName) : null;
        // 这三条都是"这条消息没人处理"，往上抛而不是记一条日志就 return：两个调用方都是
        // HTTP 端点，return 会让它们回 200，投递方据此认定消息已处理完 —— 而这条消息
        // 根本没落库，就此静默消失。getChannelRegistry().get 未知 channel 自己就抛。
        if (!botConfig) {
            throw new Error(
                `bot config not found for "${botName}"; cannot handle inbound ` +
                    `qq_message_id=${custom?.messageId}`,
            );
        }
        const botCommonUserId = botConfig.common_user_id;
        if (!botCommonUserId) {
            throw new Error(
                `bot "${botName}" has no common_user_id; bot identity initialization must ` +
                    `run before inbound handling (qq_message_id=${custom?.messageId})`,
            );
        }
        const plugin = getChannelRegistry().get(botConfig.channel);

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

        // 锁里做投影、泳道判定和本 lane 的副作用；判出「不归本进程」时只把信封
        // 带出来，交接在锁外做（见下方 handOffToLane 处）。
        const handoff = await withQqInboundProjectionLock(custom.messageId, async () => {
            const projection = await prepareQqInboundProjection(inbound, botName, botCommonUserId);

            const pendingHandoff = await resolveInboundLaneHandoff({
                handedOff: options.handedOff === true,
                currentLane: getLane() ?? 'prod',
                channel: botConfig.channel,
                botGlobalId: botName,
                commonConversationId: projection.commonConversationId,
                eventType: 'qq.message.receive',
                globalMessageId: projection.commonMessageId,
                traceId: context.getTraceId(),
                params: custom,
            });
            if (pendingHandoff) return pendingHandoff;

            // ---- 分界后：仅本 lane 执行的副作用 ----
            upsertCommonBotPresence(projection.commonConversationId, botName, true).catch((err) =>
                console.warn('[qq CommonBotPresence] upsert failed:', err),
            );

            enqueueQqImagePipeline(inbound, projection.commonMessageId, botName);

            const ruleMessage = buildQqRuleMessage(inbound, {
                botName,
                commonUserId: projection.commonUserId,
                commonConversationId: projection.commonConversationId,
                commonMessageId: projection.commonMessageId,
                commonRootMessageId: projection.commonRootMessageId,
                botCommonUserId,
                mentionedUserIds: projection.mentionedUserIds,
            });

            // 指令走这里。QQ 今天没有平台指令，所以它的产出只有一条终态日志。
            await runRules(ruleMessage);

            // 落库失败必须上抛。吞掉它的话调用方会照常回 2xx —— 投递方于是认为这条
            // 消息处理完了，实际它根本不在库里，而她能读到的只有库里那份。
            try {
                await storeQqInboundMessage(
                    inbound,
                    projection,
                    custom as unknown as Record<string, unknown>,
                );
            } catch (storeErr) {
                throw new Error(
                    `storing the inbound qq message failed: ` +
                        `message=${projection.commonMessageId} ` +
                        `chat=${projection.commonConversationId}: ` +
                        `${(storeErr as Error).message}`,
                    { cause: storeErr },
                );
            }

            return null;
        });

        // 交接在锁外：投递是一次同步等对端处理完的跨进程调用，而这把锁在 Redis 上、
        // prod 与泳道进程共用同一个。持锁等外部返回，接收端重走投影去抢同一条消息的锁
        // 时两边就会互等到窗口超时。
        if (handoff) await handOffToLane(handoff);
    }
}

export const qqEventHandlers = new QqEventHandlers();
