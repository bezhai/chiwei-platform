// 两个图片日报的任务本体。挂在哪、什么时候跑、以谁的身份跑，全在 src/schedule.ts。
//
//     18:00  每日一图   随机一张 → 订阅群；隔十秒再往另一个群发一句话 + 把卡片挂上去
//     19:30  今日新图   昨天这个时刻之后入库的图汇成一张卡片 → 第三个群
//
// ## 群 id 是写死的，而且必须写死
//
// 这三个 id 没有任何配置来源，拆分前就是字面量。参数化是新设计不是迁移，所以照搬。
// 它们也是这批代码里最不能错的三个字符串：发错群不会报错，只会发到别人那儿去。
//
// ## 它们必须待在单副本的那个进程里
//
// 出站进程是竞争消费、可以多副本，每个副本各起一份 cron 就是往这三个真实的群各发
// N 遍。所以定时任务归入口进程（见 src/schedule.ts 的文件头）。
//
// ## 两个任务对错误的态度不一样，这是照搬不是笔误
//
// 「每日一图」把错误抛给基座（基座会记一条 failed）；「今日新图」自己吞掉记一条
// error，于是它失败时基座那条 "finished" 照常打出来。拆分前就是这个形状。

import dayjs from 'dayjs';
import { CardHeader, ImgComponent, LarkCard } from 'feishu-card';
import { StatusMode } from '@inner/pixiv-client';

import type { LarkOutboundApi } from '../outbound/lark-api';
import { newPhotoCard } from './cards';
import type { LarkReadyPhotos } from './ready';

/** 每日一图的订阅群。 */
export const DAILY_PHOTO_CHAT = 'oc_0d2e26c81fdf0823997a7bb40d71dcc1';
/** 先收到一句「每日一图」、再收到挂在它上面的卡片的那个群。 */
export const DAILY_PHOTO_NUDGE_CHAT = 'oc_a44255e98af05f1359aeb29eeb503536';
/** 今日新图那个群。 */
export const DAILY_NEW_PHOTO_CHAT = 'oc_a79ce7cc8cc4afdcfd519532d0a917f5';

/** 第二个群那两条消息之间的停顿。拆分前就是 10 秒。 */
const NUDGE_GAP_MS = 10_000;

export interface DailyPhotoDeps {
    api: Pick<LarkOutboundApi, 'sendCard' | 'sendText' | 'replyCard'>;
    photos: LarkReadyPhotos;
    /** 停一会儿。测试传一个立刻返回的 —— 定时任务的测试不许真的等时间流逝。 */
    wait: (ms: number) => Promise<void>;
    /** 现在几点。「今日新图」按它往回推一天。 */
    now: () => Date;
}

/** 18:00：随机一张图，两个群各收到一次。 */
export function dailyPhoto(deps: DailyPhotoDeps): () => Promise<void> {
    return async () => {
        const photos = await deps.photos({
            status: StatusMode.VISIBLE,
            page: 1,
            page_size: 1,
            random_mode: true,
        });
        // 一张都没有就整个跳过：不发空卡片，也不发那句「每日一图」。
        if (photos.length <= 0) return;

        const photo = photos[0]!;
        const card = new LarkCard()
            .withHeader(new CardHeader('今天的每日一图').color('blue'))
            .addElement(new ImgComponent(photo.image_key!).setAlt(photo.pixiv_addr));

        await deps.api.sendCard(DAILY_PHOTO_CHAT, card);

        // 第二个群的玩法：先发一句话，等一会儿，再把卡片挂在那句上（进话题）。
        await deps.wait(NUDGE_GAP_MS);
        const nudge = await deps.api.sendText(DAILY_PHOTO_NUDGE_CHAT, '每日一图');
        if (!nudge.messageId) {
            // 平台偶尔回 code=0 却不带 id。没有它就挂不上去 —— 说清楚，让基座记一条
            // 查得到的 failed，而不是拿 undefined 去打飞书换一个跟日报无关的报错。
            throw new Error(
                'lark accepted the 每日一图 nudge but returned no message id; ' +
                    'the card cannot be attached to it',
            );
        }
        await deps.api.replyCard(nudge.messageId, card, true);
    };
}

/** 19:30：昨天这个时刻之后入库的新图。 */
export function dailyNewPhoto(deps: DailyPhotoDeps): () => Promise<void> {
    return async () => {
        try {
            // 定时任务不是被谁触发的，没有会话开关可读 —— 永远按"仅可见"取。
            const card = await newPhotoCard(
                deps.photos,
                dayjs(deps.now()).add(-1, 'day').valueOf(),
                undefined,
            );
            await deps.api.sendCard(DAILY_NEW_PHOTO_CHAT, card);
        } catch (error) {
            console.error('[lark-photo] daily new photo failed:', error);
        }
    };
}
