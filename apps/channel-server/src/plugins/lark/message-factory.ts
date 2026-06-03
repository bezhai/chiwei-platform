import type { LarkReceiveMessage, LarkHistoryMessage } from 'types/lark';
import type { MessageContent } from '@core/models/message-content';
import { ContentType, type ContentItem } from '@core/models/message-content';
import type { MessageMetadata } from '@core/models/message-metadata';
import { Message } from '@core/models/message';
import {
    BaseChatInfoRepository,
    GroupChatInfoRepository,
    UserRepository,
} from '@infrastructure/dal/repositories/repositories';

async function buildMetadataFromEvent(event: LarkReceiveMessage): Promise<MessageMetadata> {
    const [basicChatInfo, groupChatInfo, senderInfo] = await Promise.all([
        event.message.chat_type === 'p2p'
            ? BaseChatInfoRepository.findOne({
                  where: { chat_id: event.message.chat_id },
              })
            : null,
        event.message.chat_type !== 'p2p'
            ? GroupChatInfoRepository.findOne({
                  where: { chat_id: event.message.chat_id },
                  relations: ['baseChatInfo'],
              })
            : null,
        event.sender.sender_id?.union_id
            ? UserRepository.findOne({
                  where: { union_id: event.sender.sender_id.union_id },
              })
            : null,
    ]);

    const finalBasicChatInfo =
        event.message.chat_type !== 'p2p' ? (groupChatInfo?.baseChatInfo ?? null) : basicChatInfo;

    return {
        messageId: event.message.message_id,
        chatId: event.message.chat_id,
        sender: event.sender.sender_id?.union_id ?? 'unknown_sender',
        senderOpenId: event.sender.sender_id?.open_id,
        parentMessageId: event.message.parent_id,
        chatType: event.message.chat_type,
        rootId: event.message.root_id || event.message.message_id,
        threadId: event.message.thread_id,
        isRobotMessage: false,
        messageType: event.message.message_type,
        basicChatInfo: finalBasicChatInfo ?? undefined,
        groupChatInfo: groupChatInfo ?? undefined,
        senderInfo: senderInfo ?? undefined,
        createTime: event.message.create_time,
    };
}

function buildMetadataFromHistory(message: LarkHistoryMessage): MessageMetadata {
    return {
        messageId: message.message_id!,
        chatId: message.chat_id!,
        sender: message.sender?.id ?? 'unknown',
        parentMessageId: message.parent_id,
        chatType: 'group',
        rootId: message.root_id,
        threadId: message.thread_id,
        isRobotMessage: message.sender?.id_type === 'app_id',
        createTime: message.create_time,
    };
}

function buildContentFromHistory(message: LarkHistoryMessage): MessageContent {
    try {
        const content = JSON.parse(message.body?.content ?? '{}');
        const items: ContentItem[] = [];

        if (content.text) {
            items.push({ type: ContentType.Text, value: content.text });
        }

        return {
            items,
            mentions: (message.mentions ?? []).map((m) => ({
                id: m.id,
                displayName: m.name ?? m.id,
            })),
        };
    } catch (error) {
        console.error('Error parsing history message content:', error);
        return { items: [], mentions: [] };
    }
}

export async function createLarkMessageFromEvent(
    event: LarkReceiveMessage,
    content: MessageContent,
): Promise<Message> {
    const metadata = await buildMetadataFromEvent(event);
    return new Message(metadata, content);
}

export function createLarkMessageFromHistory(message: LarkHistoryMessage): Message {
    const metadata = buildMetadataFromHistory(message);
    const content = buildContentFromHistory(message);
    return new Message(metadata, content);
}
