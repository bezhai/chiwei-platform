import { ChatMessage } from 'types/chat';
import { ConversationMessageRepository } from 'infrastructure/dal/repositories/repositories';
import { xadd } from 'infrastructure/cache/redis-client';
import { context } from '@middleware/context';

// Redis Stream 名称，用于向量化任务队列
const VECTORIZE_STREAM = 'vectorize_stream';

/**
 * 判断消息内容是否为空
 * 空消息定义：content 为空或仅包含空白字符
 */
function isEmptyContent(content: string | undefined | null): boolean {
    return !content || content.trim() === '';
}

/**
 * 存储消息到 PostgreSQL 并推送向量化任务到 Redis Stream
 *
 * 解耦设计：
 * 1. 消息直接写入 PostgreSQL，不依赖 ai-service
 * 2. 向量化任务通过 Redis Stream 异步处理
 * 3. 即使 ai-service 不可用，消息也不会丢失
 * 4. 空消息不推送到向量化队列，直接标记为 skipped
 */
export async function storeMessage(message: ChatMessage): Promise<void> {
    try {
        // 获取当前上下文中的 bot_name（用于后续图片下载等操作），默认 bytedance
        const botName = message.bot_name || context.getBotName() || 'bytedance';

        // 判断是否为空消息
        const isEmpty = isEmptyContent(message.content);

        // 1. 直接写入 PostgreSQL
        await ConversationMessageRepository.save({
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
        });

        // 2. 仅非空消息推送向量化任务到 Redis Stream
        if (!isEmpty) {
            await xadd(VECTORIZE_STREAM, '*', 'message_id', message.message_id);
        }
    } catch (error: unknown) {
        console.error('Failed to store message:', (error as Error).message);
    }
}
