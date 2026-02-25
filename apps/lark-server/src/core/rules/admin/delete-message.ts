import { getMessageInfo, deleteMessage } from '@lark-client';
import { Message } from 'core/models/message';
import { getBotAppId } from '@core/services/bot/bot-var';
import { replyMessage } from '@lark/basic/message';

export async function deleteBotMessage(message: Message) {
    try {
        if (!message.parentMessageId) {
            throw new Error('没有父消息，无法撤回');
        }

        const parentMessageInfo = await getMessageInfo(message.parentMessageId);

        if (
            !parentMessageInfo.items ||
            parentMessageInfo.items.length === 0 ||
            !parentMessageInfo.items[0]
        ) {
            throw new Error('父消息为空，无法撤回');
        }

        const parentMessage = parentMessageInfo.items[0];

        if (parentMessage?.sender?.id !== getBotAppId()) {
            // 这里拿到的不是union_id, 而是app_id
            throw new Error('只能撤回机器人自己发送的消息');
        }

        await deleteMessage(message.parentMessageId);
    } catch (e) {
        const errorMessage = e instanceof Error ? e.message : '未知错误';
        replyMessage(message.messageId, `撤回失败: ${errorMessage}`, true);
        console.error('delete bot message error:', {
            message: e instanceof Error ? e.message : 'Unknown error',
            stack: e instanceof Error ? e.stack : undefined,
        });
    }
}
