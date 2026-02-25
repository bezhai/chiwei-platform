import { LarkReceiveMessage } from 'types/lark';
import { LarkMessageMetaInfo, LarkUserMessageMetaInfo } from 'types/mongo';
import { LarkHistoryMessage } from 'types/lark';
import { MessageMetadata } from './message-metadata';
import { MessageContent, ContentType, ContentItem } from './message-content';
import { MentionUtils } from '@lark/utils/mention-utils';
import {
    BaseChatInfoRepository,
    GroupChatInfoRepository,
    UserRepository,
} from '@infrastructure/dal/repositories/repositories';
import { MessageTransferer } from '@lark/events/factory';

export class MessageBuilder {
    static async buildMetadataFromEvent(event: LarkReceiveMessage): Promise<MessageMetadata> {
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
            event.message.chat_type !== 'p2p'
                ? (groupChatInfo?.baseChatInfo ?? null)
                : basicChatInfo;

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

    static buildMetadataFromInfo(message: LarkMessageMetaInfo): MessageMetadata {
        return {
            messageId: message.message_id,
            chatId: message.chat_id,
            sender: message.is_from_robot ? 'robot' : (message.sender ?? 'unknown_sender'),
            parentMessageId: message.parent_id,
            chatType: message.chat_type,
            rootId: message.root_id,
            threadId: message.thread_id,
            isRobotMessage: message.is_from_robot,
            createTime: message.create_time.getTime().toString(), // 转换为毫秒时间戳字符串
        };
    }

    static buildMetadataFromHistory(message: LarkHistoryMessage): MessageMetadata {
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

    static buildContentFromInfo(message: LarkMessageMetaInfo): MessageContent {
        try {
            const items: ContentItem[] = [];

            if (message.is_from_robot) {
                // Robot message
                if (message.robot_text) {
                    items.push({ type: ContentType.Text, value: message.robot_text });
                }
                return {
                    items,
                    mentions: [],
                };
            } else {
                // User message
                const userMessage = message as LarkUserMessageMetaInfo;
                if (userMessage.message_content) {
                    items.push(
                        ...MessageTransferer.getContentFactory(
                            userMessage.message_type,
                            userMessage.message_content,
                        ).generateContent(),
                    );
                }
                return {
                    items,
                    mentions: MentionUtils.addMentions(userMessage.mentions),
                };
            }
        } catch (error) {
            console.error('Error parsing message info content:', error);
            return { items: [], mentions: [] };
        }
    }

    static buildContentFromHistory(message: LarkHistoryMessage): MessageContent {
        try {
            const content = JSON.parse(message.body?.content ?? '{}');
            const items: ContentItem[] = [];

            if (content.text) {
                items.push({ type: ContentType.Text, value: content.text });
            }

            return {
                items,
                mentions: (message.mentions ?? []).map((m) => m.id),
            };
        } catch (error) {
            console.error('Error parsing history message content:', error);
            return { items: [], mentions: [] };
        }
    }
}
