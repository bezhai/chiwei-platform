import { sendReq } from '@lark-client';
import { BaseChatInfoRepository } from 'infrastructure/dal/repositories/repositories';
import { LarkCallbackInfo } from 'types/lark';
import { replyCard } from '@lark/basic/message';
import { getPhotoDetailCard } from '@core/services/media/photo/photo-card';

export async function fetchAndSendPhotoDetail(data: LarkCallbackInfo, pixivAddrs: string[]) {
    try {
        const basicChatInfoPromise = BaseChatInfoRepository.findOne({
            where: { chat_id: data.context.open_chat_id },
        });

        const detailCardPromise = getPhotoDetailCard(pixivAddrs);

        const [basicChatInfo, detailCard] = await Promise.all([
            basicChatInfoPromise,
            detailCardPromise,
        ]);

        if (basicChatInfo?.chat_mode === 'p2p' || !basicChatInfo) {
            await replyCard(data.context.open_message_id, detailCard);
        } else {
            // 群聊下需要发送到指定用户
            await sendReq(
                `/open-apis/ephemeral/v1/send`,
                {
                    chat_id: data.context.open_chat_id,
                    msg_type: 'interactive',
                    card: detailCard,
                    open_id: data.operator.open_id,
                },
                'POST',
            );
        }
    } catch (e) {
        console.error('fetch photo detail error:', {
            message: e instanceof Error ? e.message : 'Unknown error',
            stack: e instanceof Error ? e.stack : undefined,
        });
    }
}
