import { Message } from 'core/models/message';
import { context } from '@middleware/context';
import { v4 as uuidv4 } from 'uuid';
import { AgentResponseRepository } from '@repositories/repositories';
import { AgentResponse } from '@entities/agent-response';
import { rabbitmqClient, CHAT_REQUEST, getLane } from '@integrations/rabbitmq';
import { setNx } from '@cache/redis-client';

/**
 * 队列模式回复：发布 chat.request 到 RabbitMQ，立即返回。
 * agent-service 异步处理后发布 chat.response，由 chat-response-worker 消费并发送。
 */
export async function makeTextReply(message: Message): Promise<void> {
    // 多 bot 场景去重：同一消息只有第一个拿到锁的 bot 继续，其余静默返回
    const lock = await setNx(`make_reply:${message.messageId}`, '1', 60);
    if (lock === null) {
        console.info(`[makeTextReply] Skipping duplicate message_id=${message.messageId}`);
        return;
    }

    const sessionId = uuidv4();

    // 创建 agent_responses 记录
    try {
        const agentResponse = AgentResponseRepository.create({
            session_id: sessionId,
            trigger_message_id: message.messageId,
            chat_id: message.chatId,
            bot_name: context.getBotName() || undefined,
            status: 'pending',
        } as Partial<AgentResponse>);
        await AgentResponseRepository.save(agentResponse);
    } catch (e) {
        console.error('Failed to create agent_response:', e);
    }

    // 发布到 chat.request 队列
    const lane = context.getLane() || getLane() || undefined;
    await rabbitmqClient.publish(
        CHAT_REQUEST,
        {
            session_id: sessionId,
            message_id: message.messageId,
            chat_id: message.chatId,
            is_p2p: message.isP2P(),
            root_id: message.rootId,
            user_id: message.senderInfo?.union_id,
            bot_name: context.getBotName(),
            is_canary: message.basicChatInfo?.permission_config?.is_canary ?? false,
            lane: lane || undefined,
            enqueued_at: Date.now(),
            mentions: message.getMentionedUsers(),
        },
        undefined,
        undefined,
        lane,
    );

    console.info(
        `[makeTextReply] Published chat.request: session_id=${sessionId}, message_id=${message.messageId}`,
    );
}
