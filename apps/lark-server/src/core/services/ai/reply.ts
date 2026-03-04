import { Message } from 'core/models/message';
import { MessageContentUtils } from 'core/models/message-content';
import { sseChat } from './chat';
import { CardLifecycleManager } from '@lark/basic/card-lifecycle-manager';
import { getBotUnionId } from '@core/services/bot/bot-var';
import { context } from '@middleware/context';
import dayjs from 'dayjs';
import { v4 as uuidv4 } from 'uuid';
import { AgentResponseRepository } from '@repositories/repositories';
import { AgentResponse } from '@entities/agent-response';
import { rabbitmqClient, RK_CHAT_REQUEST, getLane } from '@integrations/rabbitmq';

/**
 * 队列模式回复：发布 chat.request 到 RabbitMQ，立即返回。
 * agent-service 异步处理后发布 chat.response，由 chat-response-worker 消费并发送。
 */
export async function makeTextReply(message: Message): Promise<void> {
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
    const lane = getLane();
    await rabbitmqClient.publish(
        RK_CHAT_REQUEST,
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
        },
        undefined,
        undefined,
        lane,
    );

    console.info(
        `[makeTextReply] Published chat.request: session_id=${sessionId}, message_id=${message.messageId}`,
    );
}

/**
 * 重试已有卡片（仍使用 SSE + 卡片模式，因为它是重试现有卡片）
 */
export async function reCreateCard(
    messageId: string,
    parentMessageId: string,
    chatId: string,
    rootId: string,
    isP2P: boolean,
): Promise<void> {
    const cardManager = await CardLifecycleManager.loadFromMessage(messageId);

    if (!cardManager) {
        return;
    }

    cardManager.appendCardContext({
        parent_message_id: parentMessageId,
        chat_id: chatId,
        root_id: rootId,
        is_p2p: isP2P,
    });

    const onSaveMessage = async (content: string) => {
        if (!cardManager.getMessageId()) {
            return undefined;
        }
        return {
            user_id: getBotUnionId(),
            user_name: '赤尾',
            content: MessageContentUtils.wrapMarkdownAsV2(content),
            is_mention_bot: false,
            role: 'assistant',
            message_id: cardManager.getMessageId()!,
            message_type: 'post',
            chat_id: chatId,
            chat_type: isP2P ? 'p2p' : 'group',
            create_time: String(dayjs(cardManager.getCreateTime()).valueOf()),
            root_message_id: rootId,
            reply_message_id: parentMessageId,
        } as const;
    };

    await sseChat({
        req: {
            message_id: parentMessageId,
        },
        ...cardManager.createAdvancedCallbacks(parentMessageId),
        onStartReply: async () => {},
        onSaveMessage,
    });
}
