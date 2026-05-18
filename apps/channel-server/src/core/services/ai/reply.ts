import { context } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';
import { AgentResponseRepository } from '@repositories/repositories';
import { AgentResponse } from '@entities/agent-response';
import { rabbitmqClient, CHAT_REQUEST, getLane } from '@integrations/rabbitmq';
import { setNx } from '@cache/redis-client';
import type { RuleMessage } from 'core/rules/rule-message';

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
 * 队列模式回复：发布 chat.request 到 RabbitMQ，立即返回。
 * agent-service 异步处理后发布 chat.response，由 chat-response-worker 消费并发送。
 *
 * persona 文本主链路 —— 决策五里唯一真正平台无关的规则，消费 RuleMessage。
 */
export async function makeTextReply(message: RuleMessage): Promise<void> {
    // 多 bot 场景去重：同一消息只有第一个拿到锁的 bot 继续，其余静默返回。
    // 锁键用全局 internal_message_id（跨 channel 唯一，不会撞）。
    const lock = await setNx(`make_reply:${message.internalMessageId}`, '1', 60);
    if (lock === null) {
        console.info(
            `[makeTextReply] Skipping duplicate message_id=${message.internalMessageId}`,
        );
        return;
    }

    const sessionId = uuidv4();
    const botName = context.getBotName() || undefined;

    // 创建 agent_responses 记录
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

    const lane = context.getLane() || getLane() || undefined;
    const payload = buildChatRequestPayload(message, sessionId, botName, lane);

    await rabbitmqClient.publish(
        CHAT_REQUEST,
        payload as unknown as Record<string, unknown>,
        undefined,
        undefined,
        lane,
    );

    console.info(
        `[makeTextReply] Published chat.request: session_id=${sessionId}, ` +
            `message_id=${message.internalMessageId}, channel=${message.channel}, ` +
            `lane=${lane || 'prod'}`,
    );
}
