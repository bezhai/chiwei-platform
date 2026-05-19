import { ChatMessage } from 'types/chat';
import { ConversationMessage } from '@entities/conversation-message';
import { context } from '@middleware/context';
import { rabbitmqClient, VECTORIZE } from '@integrations/rabbitmq';
import AppDataSource from 'ormconfig';

function isEmptyContent(content: string | undefined | null): boolean {
    return !content || content.trim() === '';
}

/**
 * 存储消息到 PostgreSQL 并推送向量化任务到 RabbitMQ
 *
 * 使用 INSERT ... ON CONFLICT DO NOTHING 实现原子去重：
 * - 多 bot 同群时，同一 message_id 只有第一个到达的 bot 能成功插入
 * - 仅插入成功时推送向量化任务，天然防止重复向量化
 *
 * 失败语义（fail-loud）：真实 DB 故障（连接失败、超时、非预期错误）直接
 * 抛出，**不在此吞掉**。入站 handlers.ts 据此 fail-loud（不 savePending /
 * 不 publish），否则下游 agent-service find_message_content 读空回
 * "未找到记录"。ON CONFLICT/重复不是故障：TypeORM `.orIgnore()` 在 SQL
 * 层由 PG 自身吃掉冲突，execute() 正常返回、identifiers 为空（行已存在、
 * 可回查），视为成功，不会走到任何错误路径。需要"失败不致命"的调用方
 * （如出站 chat-response-worker）自己 try/catch 决定降级，不靠这里统一吞。
 */
export async function storeMessage(message: ChatMessage): Promise<void> {
    const botName = message.bot_name || context.getBotName() || 'chiwei';
    const isEmpty = isEmptyContent(message.content);

    const result = await AppDataSource.createQueryBuilder()
        .insert()
        .into(ConversationMessage)
        .values({
            message_id: message.message_id,
            user_id: message.user_id,
            username: message.username ?? undefined,
            content: message.content,
            role: message.role,
            root_message_id: message.root_message_id || message.message_id,
            reply_message_id: message.reply_message_id,
            chat_id: message.chat_id,
            chat_type: message.chat_type,
            create_time: message.create_time,
            message_type: message.message_type || 'text',
            bot_name: botName,
            response_id: message.response_id,
        })
        .orIgnore()
        .execute();

    const inserted = result.identifiers.length > 0;

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
}
