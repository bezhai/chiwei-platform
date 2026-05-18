import { createHash } from 'node:crypto';
import { get, setWithExpire } from 'infrastructure/cache/redis-client';
import { BaseChatInfoRepository } from 'infrastructure/dal/repositories/repositories';
import { Message } from 'core/models/message';
import { sendSticker, replyMessage, sendPost } from '@lark/basic/message';
import { createPostContentFromText } from '@core/services/message/post-content-processor';

interface RepeatMsg {
    chatId: string;
    msg: string;
    repeatTime: number;
}

async function addRepeatMsgAndCheck(chatId: string, msg: string): Promise<boolean> {
    // 消息体的 Redis 键名
    const redisKey = `repeat_msg:${chatId}`;

    // 对消息进行 MD5 哈希
    const hashedMsg = createHash('md5').update(msg).digest('hex');

    // 从 Redis 获取当前的消息记录
    const existingData = await get(redisKey);
    let msgBody: RepeatMsg;

    if (existingData) {
        // 如果 Redis 中已有记录，解析 JSON 数据
        msgBody = JSON.parse(existingData) as RepeatMsg;

        if (msgBody.msg === hashedMsg) {
            // 如果消息相同，增加重复次数
            msgBody.repeatTime++;
        } else {
            // 如果消息不同，重置为新的消息
            msgBody = {
                chatId,
                msg: hashedMsg,
                repeatTime: 1,
            };
        }
    } else {
        // 如果 Redis 中没有记录，初始化消息体
        msgBody = {
            chatId,
            msg: hashedMsg,
            repeatTime: 1,
        };
    }

    // 更新 Redis 数据，设置过期时间为 7 天
    await setWithExpire(redisKey, JSON.stringify(msgBody), 7 * 24 * 60 * 60);

    // 返回是否达到重复次数 3 的条件
    return msgBody.repeatTime === 3;
}

export async function repeatMessage(message: Message) {
    if (
        message.isTextOnly() &&
        (await addRepeatMsgAndCheck(message.chatId, message.withMentionText()))
    ) {
        const mentionText = message.withMentionText();
        const postContent = await createPostContentFromText(mentionText);
        sendPost(message.chatId, postContent);
    } else if (
        message.isStickerOnly() &&
        (await addRepeatMsgAndCheck(message.chatId, message.stickerKey()))
    ) {
        sendSticker(message.chatId, message.stickerKey());
    }
}

export function changeRepeatStatus(
    open_repeat_message: boolean,
): (message: Message) => Promise<void> {
    return async function (message: Message) {
        await BaseChatInfoRepository.createQueryBuilder()
            .update()
            .set({
                permission_config: () =>
                    `COALESCE(permission_config, '{}'::jsonb) || '{"open_repeat_message": ${open_repeat_message}}'`,
            })
            .where('chat_id = :chatId', { chatId: message.chatId })
            .execute();
        if (open_repeat_message) {
            replyMessage(
                message.messageId,
                `呜哇~复读功能已经开启啦！如果在群聊里看到同样的文字或表情连续出现三次的话，人家也会跟着一起复读呢！(。>︿<)_θ`,
            );
        } else {
            replyMessage(
                message.messageId,
                `诶嘿~复读功能已经关闭啦！人家暂时就不会复读了呢 (｡•́︿•̀｡)`,
            );
        }
    };
}
