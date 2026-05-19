import { context } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';
import { AgentResponseRepository } from '@repositories/repositories';
import { AgentResponse } from '@entities/agent-response';
import { getLane } from '@integrations/rabbitmq';
import type { RuleMessage } from 'core/rules/rule-message';
import type { RuleHandlerContext } from 'core/rules/engine';

// chat.request 载荷。message_id/chat_id/root_id/user_id 一律是全局
// internal_*_id（RuleMessage 上已是 IdentityResolver.resolve 之后的全局 ID，
// 不再绕 channel-binding context 退回飞书裸 ID）。agent-service 对 channel
// 无感知、只透传，ChatResponseSegment 原路带回。
export interface ChatRequestPayload {
    session_id: string;
    channel: string;
    message_id: string;
    chat_id: string;
    is_p2p: boolean;
    root_id: string;
    user_id: string;
    bot_name: string | undefined;
    is_canary: boolean;
    lane: string | undefined;
    enqueued_at: number;
    mentions: string[];
}

// 纯函数：从平台无关 RuleMessage 构造 chat.request 载荷。is_canary / mentions
// 是飞书专属语义——仅当 channelContext 是 lark 时从 LarkRuleContext 取，其余
// channel 取中性默认（false / []），绝不把飞书绑定泄漏到非飞书 channel。
export function buildChatRequestPayload(
    message: RuleMessage,
    sessionId: string,
    botName: string | undefined,
    lane: string | undefined,
): ChatRequestPayload {
    let isCanary = false;
    let mentions: string[] = [];
    const ctx = message.channelContext;
    if (ctx && ctx.channel === 'lark') {
        const lark = ctx.larkMessage;
        isCanary = lark.basicChatInfo?.permission_config?.is_canary ?? false;
        mentions = lark.getBotAppIds();
    }
    return {
        session_id: sessionId,
        channel: message.channel,
        message_id: message.internalMessageId,
        chat_id: message.internalChatId,
        is_p2p: message.isDirect,
        root_id: message.internalRootId ?? message.internalMessageId,
        user_id: message.internalUserId,
        bot_name: botName,
        is_canary: isCanary,
        lane,
        enqueued_at: Date.now(),
        mentions,
    };
}

/**
 * persona 文本主链路 —— 决策五里唯一真正平台无关的规则，消费 RuleMessage。
 *
 * 5b 入站重排（决策一/二）：本 handler 在 runRules 阶段**不实际 publish**、
 * **不取去重锁**、**不落 agent_responses pending 行**。它只做平台无关的纯
 * 预备工作（生成 session_id、构造 chat.request 载荷、构造 pending 行落库
 * 闭包），把"待发 ChatTrigger 意图"经 ctx.registerPendingChatTrigger 登记
 * 给引擎，由接线点 handlers.ts 在 storeMessage **成功之后**取锁、抢到锁才
 * 落 pending 行并发 MQ —— 保证下游 agent-service find_message_content
 * (message_id) 先存后查、不读空走"未找到消息记录"短路；去重锁、pending
 * 行落库、publish 三者紧邻（避免拿锁后 storeMessage 失败导致锁空占 60s）。
 *
 * 必改2：agent_responses pending 行 save 后移到抢锁之后。重排前 setNx 在
 * pending save 之前、未抢锁的 bot 直接 return 不 save；若 pending save 仍
 * 留在 runRules 阶段（早于 handlers 后移的 setNx），多 bot 同群处理同一
 * 全局 message_id 时每个 bot 都写一条 pending 行、但只有抢锁的才 publish
 * → 未抢锁 bot 留下永不完成的孤儿 pending 行。故 save 闭包化、由 handlers
 * 抢锁后调用。AgentResponse 仓储逻辑仍只在本文件一处（闭包内），不泄漏到
 * handlers。pending 行 ⇎ 已发 MQ 不是系统不变量（消费方按 session_id /
 * trigger_message_id 查，只有 chat.response 真回来才命中，而那必在
 * publish 之后），故 save 时序后移安全（见回报 grep 求证）。
 */
export async function makeTextReply(
    message: RuleMessage,
    ctx?: RuleHandlerContext,
): Promise<void> {
    const sessionId = uuidv4();
    const botName = context.getBotName() || undefined;

    const lane = context.getLane() || getLane() || undefined;
    const payload = buildChatRequestPayload(message, sessionId, botName, lane);

    // agent_responses pending 行落库闭包（必改2）：仓储逻辑只在此一处，
    // 但**不在此执行**。由 handlers.ts 抢到去重锁后调用，与 publish 原子
    // 相邻。落库失败仍记可查日志、不抛（与重排前同语义：pending 行只是
    // 观测便利，不是发 MQ 的前置不变量）。
    const savePending = async (): Promise<void> => {
        try {
            const agentResponse = AgentResponseRepository.create({
                session_id: sessionId,
                trigger_message_id: message.internalMessageId,
                chat_id: message.internalChatId,
                bot_name: botName,
                status: 'pending',
            } as Partial<AgentResponse>);
            await AgentResponseRepository.save(agentResponse);
        } catch (e) {
            console.error('Failed to create agent_response:', e);
        }
    };

    // 登记待发意图（决策一）。dedupeKey 用全局 internal_message_id
    // （跨 channel 唯一），取锁、pending 落库、publish 由 handlers.ts 在
    // storeMessage 成功后紧邻执行。ctx 缺失（理论上不会，handlers 必传）
    // 则不登记 —— 防御性健壮，绝不退回旧的"handler 内直接 publish"。
    ctx?.registerPendingChatTrigger({
        payload,
        lane,
        dedupeKey: `make_reply:${message.internalMessageId}`,
        savePending,
    });

    console.info(
        `[makeTextReply] Registered pending chat.request: session_id=${sessionId}, ` +
            `message_id=${message.internalMessageId}, channel=${message.channel}, ` +
            `lane=${lane || 'prod'}`,
    );
}
