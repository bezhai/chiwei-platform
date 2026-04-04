import { ChatMessage } from 'types/chat';
import { ConversationMessage } from '@entities/conversation-message';
import { context } from '@middleware/context';
import { rabbitmqClient, VECTORIZE } from '@integrations/rabbitmq';
import AppDataSource from 'ormconfig';

/**
 * 判断消息内容是否为空
 * 空消息定义：content 为空或仅包含空白字符
 */
function isEmptyContent(content: string | undefined | null): boolean {
    return !content || content.trim() === '';
}

/**
 * 存储消息到 PostgreSQL 并推送向量化任务到 RabbitMQ
 *
 * 使用 INSERT ... ON CONFLICT DO NOTHING 实现原子去重：
 * - 多 bot 同群时，同一 message_id 只有第一个到达的 bot 能成功插入
 * - 仅插入成功时推送向量化任务，天然防止重复向量化
 */
export async function storeMessage(message: ChatMessage): Promise<void> {
    try {
        const botName = message.bot_name || context.getBotName() || 'chiwei';
        const isEmpty = isEmptyContent(message.content);

        // INSERT ... ON CONFLICT (message_id) DO NOTHING
        const result = await AppDataSource.createQueryBuilder()
            .insert()
            .into(ConversationMessage)
            .values({
                message_id: message.message_id,
                user_id: message.user_id,
                content: message.content,
                role: message.role,
                root_message_id: message.root_message_id || message.message_id,
                reply_message_id: message.reply_message_id,
                chat_id: message.chat_id,
                chat_type: message.chat_type,
                create_time: message.create_time,
                message_type: message.message_type || 'text',
                vector_status: isEmpty ? 'skipped' : 'pending',
                bot_name: botName,
                response_id: message.response_id,
            })
            .orIgnore()
            .execute();

        // orIgnore: identifiers 为空数组表示冲突未插入
        const inserted = result.identifiers.length > 0;

        // 仅首次插入成功且非空消息时推送向量化
        if (inserted && !isEmpty) {
            const lane = context.getLane() || undefined;
            await rabbitmqClient.publish(
                VECTORIZE,
                { message_id: message.message_id, lane: lane },
                undefined,
                undefined,
                lane,
            );
        }
    } catch (error: unknown) {
        console.error('Failed to store message:', (error as Error).message);
    }
}
