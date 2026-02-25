import { GroupChatInfoRepository } from 'infrastructure/dal/repositories/repositories';
import { Message } from 'core/models/message';
import { replyCard, replyMessage } from '@lark/basic/message';
import { searchAndBuildPhotoCard } from './photo-card';

export async function sendPhoto(message: Message) {
    try {
        const tags = message
            .clearText()
            .replace(/^发图/, '')
            .trim()
            .split(/\s+/)
            .filter((tag) => tag.length > 0);
        if (tags.length <= 0) {
            throw new Error('呜呜~要发图的话，记得带上标签告诉人家想看什么嘛(｡•́︿•̀｡)');
        }

        const groupInfo =
            message.basicChatInfo?.chat_mode === 'group'
                ? await GroupChatInfoRepository.findOne({
                      where: {
                          chat_id: message.chatId,
                      },
                  })
                : null;

        // 检查是否允许发送图片，满足以下任意一个条件
        // 1. 单聊
        // 2. 群聊，且人数<=20
        // 3. 群聊，且开白名单
        const allowSendPhoto =
            message.basicChatInfo?.chat_mode === 'p2p' ||
            (groupInfo && groupInfo.user_count <= 20) ||
            message.basicChatInfo?.permission_config?.allow_send_pixiv_image;

        if (!allowSendPhoto) {
            throw new Error(
                '诶嘿~这个群人有点多呢，发图功能暂时关闭啦(｡•́︿•̀｡) 想用的话可以联系开发者主人帮忙开白哦！',
            );
        }

        const photoCard = await searchAndBuildPhotoCard(
            tags,
            message.basicChatInfo?.permission_config?.allow_send_limit_photo,
        );

        await replyCard(message.messageId, photoCard);
    } catch (e) {
        const errorMessage =
            e instanceof Error
                ? e.message
                : '呜呜...好像遇到奇怪的小问题了呢 (´;ω;｀) 要不稍后再试试？';
        replyMessage(message.messageId, errorMessage, true);
        console.error('send photo error:', {
            message: e instanceof Error ? e.message : 'Unknown error',
            stack: e instanceof Error ? e.stack : undefined,
        });
    }
}
