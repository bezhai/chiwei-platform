import { StatusMode } from 'types/pixiv';
import dayjs from 'dayjs';
import {
    CardHeader,
    ImgComponent,
    LarkCard,
} from 'feishu-card';
import { fetchUploadedImages } from '@core/services/media/photo/upload';
import { replyCard, sendCard, sendMsg } from '@lark/basic/message';
import { Crontab, registerCrontabService } from '@crontab/decorators';
import { searchAndBuildDailyPhotoCard } from '@core/services/media/photo/photo-card';

export class DailyPhotoService {
    /**
     * 发图给订阅群聊
     * 每天 18:00 执行
     */
    @Crontab('0 18 * * *', { taskName: 'daily-photo', botName: 'bytedance' })
    async sendDailyPhoto(): Promise<void> {
        let images = await fetchUploadedImages({
            status: StatusMode.VISIBLE,
            page: 1,
            page_size: 1,
            random_mode: true,
        });

        if (images.length <= 0) {
            return;
        }

        const card = new LarkCard()
            .withHeader(new CardHeader('今天的每日一图').color('blue'))
            .addElement(new ImgComponent(images[0].image_key!).setAlt(images[0].pixiv_addr));

        await sendCard('oc_0d2e26c81fdf0823997a7bb40d71dcc1', card);
        await new Promise((resolve) => setTimeout(resolve, 10000));
        const msgInfo = await sendMsg('oc_a44255e98af05f1359aeb29eeb503536', '每日一图');
        await replyCard(msgInfo.message_id!, card, true);
    }

    /**
     * 发新图给特定群聊
     * 每天 19:30 执行
     */
    @Crontab('30 19 * * *', { taskName: 'daily-new-photo', botName: 'bytedance' })
    async dailySendNewPhoto(): Promise<void> {
        try {
            const card = await searchAndBuildDailyPhotoCard(dayjs().add(-1, 'day').valueOf());
            await sendCard('oc_a79ce7cc8cc4afdcfd519532d0a917f5', card);
        } catch (e) {
            console.error('daily send new photo error:', e);
        }
    }
}

// 导出单例
export const dailyPhotoService = new DailyPhotoService();

// 注册定时任务
registerCrontabService(dailyPhotoService);

