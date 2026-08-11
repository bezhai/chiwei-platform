// 三张图片卡片。
//
//     「发图 <标签>」      photoSearchCard   两栏图墙 + 换一批 / 查看详情
//     「查看详情」          photoDetailCard   一图一行，左文右图，没有按钮
//     每天 19:30 的新图     newPhotoCard      带绿色标题的两栏图墙 + 同样两个按钮
//
// 三张共用同一个取图口径（readyPhotos，保证每张都有飞书的 image_key）和同一份回调契约
// （card-actions.ts）。它们被三个入口共用 —— 指令、卡片回调、定时任务 —— 这也正是这三
// 块业务必须一起搬的原因。
//
// ## 为什么前两个交 V1、详情卡交 V2
//
// 「换一批」走的是飞书的**延时更新**接口，那个接口只吃 V1 的 elements 数组。详情卡不
// 更新自己（它是一张新发的卡），所以留在 V2。与拆分前逐字一致。
//
// ## 搜不到图**抛**，不是交一张空卡片
//
// 调用方（指令 / 回调 / 定时任务）各有各的说法：指令对着用户回一句，回调只记日志，
// 定时任务什么都不做。这一层不知道该说什么，所以把决定权抛回去。

import {
    ButtonComponent,
    CardHeader,
    Column,
    ColumnSet,
    ImgComponent,
    LarkCard,
    MarkdownComponent,
} from 'feishu-card';
import { StatusMode, type ImageForLark } from '@inner/pixiv-client';

import {
    FETCH_PHOTO_DETAILS,
    UPDATE_DAILY_PHOTO_CARD,
    UPDATE_PHOTO_CARD,
    type LarkCardActionValue,
} from './card-actions';
import { splitIntoColumns, type ColumnWeight } from './layout';
import type { LarkReadyPhotos } from './ready';

/** 一次取几张。三张卡片都是 6。 */
const PAGE_SIZE = 6;

/**
 * 「仅自己可见」那些图要不要算进来。
 *
 * 开关取值错一个方向的后果不对称：漏开只是少几张，多开是在群里发出不该发的东西。
 */
function statusFor(allowLimitPhoto: boolean | undefined): StatusMode {
    return allowLimitPhoto ? StatusMode.NOT_DELETE : StatusMode.VISIBLE;
}

function requireSome(photos: ImageForLark[]): ImageForLark[] {
    if (photos.length <= 0) throw new Error('没有找到图片');
    return photos;
}

/** 一栏图。图上的 alt 写的是 pixiv 地址，方便排查"这张是哪一张"。 */
function column(photos: ImageForLark[], weight: ColumnWeight): Column {
    return new Column()
        .setWidth('weighted', weight)
        .addElements(
            ...photos.map((photo) => new ImgComponent(photo.image_key!).setAlt(photo.pixiv_addr)),
        );
}

/** 两栏图墙。 */
function wall(photos: ImageForLark[]): ColumnSet {
    const { columns, weights } = splitIntoColumns(photos);
    return new ColumnSet()
        .setHorizontalSpacing('small')
        .addColumns(column(columns[0], weights[0]), column(columns[1], weights[1]));
}

/** 图墙底下那一排按钮。左边换一批（各卡片自己那种），右边永远是看详情。 */
function actions(refresh: LarkCardActionValue, photos: ImageForLark[]): ColumnSet {
    return new ColumnSet().addColumns(
        // 卡片库要一个开放的字典，而回调契约是个可辨识联合（那正是它的价值：读的那一头
        // 能按 type 收敛）。断言只在这一处做。
        new Column().addElements(
            new ButtonComponent()
                .setText('换一批')
                .addValue(refresh as unknown as Record<string, unknown>),
        ),
        new Column().addElements(
            new ButtonComponent().setText('查看详情').addValue({
                type: FETCH_PHOTO_DETAILS,
                // 顺序就是卡片上的顺序：详情按位置逐张对应。
                images: photos.map((photo) => photo.pixiv_addr),
            }),
        ),
    );
}

/** 「发图 <标签>」的卡片，也是「换一批」重建出来的那一张。 */
export async function photoSearchCard(
    photos: LarkReadyPhotos,
    tags: string[],
    allowLimitPhoto: boolean | undefined,
) {
    const found = requireSome(
        await photos({
            status: statusFor(allowLimitPhoto),
            page: 1,
            page_size: PAGE_SIZE,
            random_mode: true,
            tag_and_author: tags,
        }),
    );

    // 延时更新只吃 V1（见文件头）。
    return new LarkCard()
        .addElement(wall(found), actions({ type: UPDATE_PHOTO_CARD, tags }, found))
        .toV1();
}

/** 「查看详情」的卡片：卡片上那批图各占一行，左边文案右边图。 */
export async function photoDetailCard(photos: LarkReadyPhotos, pixivAddrs: string[]) {
    const found = requireSome(
        await photos({
            // ALL：卡片上那批图可能已经被删了，详情还得查得到。
            status: StatusMode.ALL,
            page: 1,
            page_size: PAGE_SIZE,
            random_mode: false,
            pixiv_addrs: pixivAddrs,
        }),
    );

    return new LarkCard().addElement(
        ...found.map((photo) => {
            // 没翻译的、标了不可见的标签都不展示 —— 前者是原文（多半是日文），后者是
            // 抓取侧刻意藏起来的。
            const tags = photo.multi_tags
                ?.filter((tag) => !!tag.translation && tag.visible)
                .map((tag) => tag.translation)
                .join('、');

            return new ColumnSet().addColumns(
                new Column()
                    .addElements(
                        new MarkdownComponent(
                            `**图片标签**：${tags}\n**作者**：${photo.author}\n**PixivId**：${photo.pixiv_addr}`,
                        ),
                    )
                    .setWidth('weighted', 1),
                new Column().addElements(
                    new ImgComponent(photo.image_key!)
                        .setAlt(photo.pixiv_addr)
                        .setSize('medium')
                        .setScaleType('crop_center'),
                ),
            );
        }),
    );
}

/** 每天 19:30 那张「今日新图」，也是它自己「换一批」重建出来的那一张。 */
export async function newPhotoCard(
    photos: LarkReadyPhotos,
    startTime: number,
    allowLimitPhoto: boolean | undefined,
) {
    const found = requireSome(
        await photos({
            status: statusFor(allowLimitPhoto),
            page: 1,
            page_size: PAGE_SIZE,
            random_mode: true,
            start_time: startTime,
        }),
    );

    return new LarkCard()
        .withHeader(new CardHeader('今日新图').color('green'))
        .addElement(
            wall(found),
            // 「换一批」带回的是时间下界而不是标签：换出来的必须还是那一天的新图。
            actions({ type: UPDATE_DAILY_PHOTO_CARD, start_time: startTime }, found),
        )
        .toV1();
}
