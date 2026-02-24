import { LarkCallbackInfo } from 'types/lark';
import { BaseChatInfoRepository } from 'infrastructure/dal/repositories/repositories';
import { sendReq } from '@lark-client';
import { searchAndBuildPhotoCard, searchAndBuildDailyPhotoCard } from '@core/services/media/photo/photo-card';

type CardBuilder = (params: any, allow_send_limit_photo?: boolean) => Promise<any>;

async function handleUpdateCard(
    data: LarkCallbackInfo,
    cardBuilder: CardBuilder,
    builderParams: any,
) {
    try {
        const basicChatInfo = await BaseChatInfoRepository.findOne({
            where: { chat_id: data.context.open_chat_id },
        });

        const updatedCard = await cardBuilder(
            builderParams,
            basicChatInfo?.permission_config?.allow_send_limit_photo,
        );

        const delayCard = {
            open_ids: [data.operator.open_id], // 非共享卡片需要更新卡片的open_ids
            elements: updatedCard.getElements(),
        };

        await sendReq(
            '/open-apis/interactive/v1/card/update',
            {
                token: data.token,
                card: delayCard,
            },
            'POST',
        );
    } catch (e) {
        console.error('update card error:', {
            message: e instanceof Error ? e.message : 'Unknown error',
            stack: e instanceof Error ? e.stack : undefined,
        });
    }
}

export async function handleUpdatePhotoCard(data: LarkCallbackInfo, tags: string[]) {
    await handleUpdateCard(data, searchAndBuildPhotoCard, tags);
}

export async function handleUpdateDailyPhotoCard(data: LarkCallbackInfo, start_time: number) {
    await handleUpdateCard(data, searchAndBuildDailyPhotoCard, start_time);
}
