// 三张图片卡片的形状，以及按钮里那份**回调契约**。
//
// 按钮的 value 是卡片和它自己的回调之间唯一的通信手段，而卡片一旦发出去就**留在飞书
// 的聊天记录里**：昨天发的卡片明天还会被人点。所以 type 字面量和字段名是线上历史的一
// 部分，改一个字就是让所有存量卡片的按钮变成哑巴（回调进来落进 unknown 分支）。

import { describe, expect, it } from 'bun:test';
import { StatusMode, type ImageForLark, type ListPixivImageDto } from '@inner/pixiv-client';

import {
    FETCH_PHOTO_DETAILS,
    UPDATE_DAILY_PHOTO_CARD,
    UPDATE_PHOTO_CARD,
} from './card-actions';
import { newPhotoCard, photoDetailCard, photoSearchCard } from './cards';
import type { LarkReadyPhotos } from './ready';

function photo(key: string, overrides: Partial<ImageForLark> = {}): ImageForLark {
    return { pixiv_addr: `${key}.png`, image_key: key, width: 100, height: 100, ...overrides };
}

function library(photos: ImageForLark[]): {
    photos: LarkReadyPhotos;
    asked: ListPixivImageDto[];
} {
    const asked: ListPixivImageDto[] = [];
    return {
        asked,
        photos: async (query) => {
            asked.push(query);
            return photos;
        },
    };
}

function json(card: object): Record<string, any> {
    return JSON.parse(JSON.stringify(card));
}

/** 卡片上所有按钮的 value，按出现顺序。 */
function buttonValues(card: object): unknown[] {
    const found: unknown[] = [];
    const walk = (node: unknown): void => {
        if (Array.isArray(node)) return void node.forEach(walk);
        if (!node || typeof node !== 'object') return;
        const record = node as Record<string, unknown>;
        if (record.tag === 'button') found.push(record.value);
        Object.values(record).forEach(walk);
    };
    walk(json(card));
    return found;
}

// ---------------------------------------------------------------------------

describe('「发图」的卡片', () => {
    it('随机取 6 张带标签的可见图', async () => {
        const it_ = library([photo('a'), photo('b')]);

        await photoSearchCard(it_.photos, ['刻晴', '原神'], undefined);

        expect(it_.asked).toEqual([
            {
                status: StatusMode.VISIBLE,
                page: 1,
                page_size: 6,
                random_mode: true,
                tag_and_author: ['刻晴', '原神'],
            },
        ]);
    });

    // 这个群开了白名单才看得到"仅自己可见"的那些图。开关取值错一个方向的后果是不
    // 对称的：漏开只是少几张，多开是在群里发出不该发的东西。
    it('开了 allow_send_limit_photo 才放宽到"没删掉的都算"', async () => {
        const it_ = library([photo('a'), photo('b')]);

        await photoSearchCard(it_.photos, ['刻晴'], true);

        expect(it_.asked[0]!.status).toBe(StatusMode.NOT_DELETE);
    });

    it('两栏图墙 + 两个按钮，交的是 V1 卡片（延时更新只认它）', async () => {
        const it_ = library([photo('a', { height: 200 }), photo('b'), photo('c')]);

        const card = json(await photoSearchCard(it_.photos, ['刻晴'], false));

        expect(card.schema).toBe('1.0');
        expect(card.elements[0].tag).toBe('column_set');
        expect(card.elements[0].horizontal_spacing).toBe('small');
        expect(card.elements[0].columns).toHaveLength(2);
        const shown = card.elements[0].columns.flatMap((column: any) =>
            column.elements.map((element: any) => element.img_key),
        );
        expect(shown.sort()).toEqual(['a', 'b', 'c']);
        expect(card.elements[1].columns.map((c: any) => c.elements[0].text.content)).toEqual([
            '换一批',
            '查看详情',
        ]);
    });

    // 「换一批」要带回原来的标签，否则换出来的是另一批毫不相干的图。
    it('按钮带回标签与这一批的地址', async () => {
        const it_ = library([photo('a'), photo('b')]);

        const values = buttonValues(await photoSearchCard(it_.photos, ['刻晴'], false));

        expect(values).toEqual([
            { type: UPDATE_PHOTO_CARD, tags: ['刻晴'] },
            { type: FETCH_PHOTO_DETAILS, images: ['a.png', 'b.png'] },
        ]);
    });

    // 一张都没搜到时**抛**，让调用方对着用户说一句 —— 发一张空卡片出去更难看。
    it('一张都没搜到就抛', async () => {
        const it_ = library([]);

        expect(photoSearchCard(it_.photos, ['不存在的标签'], false)).rejects.toThrow('没有找到图片');
    });
});

describe('「查看详情」的卡片', () => {
    it('按卡片上那批地址查，包括已经删掉的', async () => {
        const it_ = library([photo('a')]);

        await photoDetailCard(it_.photos, ['a.png', 'b.png']);

        expect(it_.asked).toEqual([
            {
                status: StatusMode.ALL,
                page: 1,
                page_size: 6,
                random_mode: false,
                pixiv_addrs: ['a.png', 'b.png'],
            },
        ]);
    });

    it('一张图一行：左边文案、右边图', async () => {
        const it_ = library([
            photo('a', {
                author: '某人',
                multi_tags: [
                    { name: 'keqing', translation: '刻晴', visible: true },
                    { name: 'hidden', translation: '藏起来的', visible: false },
                    { name: 'untranslated', visible: true },
                ],
            }),
        ]);

        const card = json(await photoDetailCard(it_.photos, ['a.png']));

        const [left, right] = card.body.elements[0].columns;
        expect(left.elements[0].content).toBe(
            '**图片标签**：刻晴\n**作者**：某人\n**PixivId**：a.png',
        );
        expect(left.weight).toBe(1);
        expect(right.elements[0].img_key).toBe('a');
        expect(right.elements[0].size).toBe('medium');
        expect(right.elements[0].scale_type).toBe('crop_center');
    });

    // 详情卡片是"仅自己可见"或者回复出去的，没有按钮 —— 有的话点了会再叠一层。
    it('详情卡片上没有按钮', async () => {
        const it_ = library([photo('a')]);

        expect(buttonValues(await photoDetailCard(it_.photos, ['a.png']))).toEqual([]);
    });

    it('一张都查不到就抛', async () => {
        const it_ = library([]);

        expect(photoDetailCard(it_.photos, ['gone.png'])).rejects.toThrow('没有找到图片');
    });
});

describe('「今日新图」的卡片', () => {
    it('按时间下界随机取 6 张', async () => {
        const it_ = library([photo('a'), photo('b')]);

        await newPhotoCard(it_.photos, 1_700_000_000_000, undefined);

        expect(it_.asked).toEqual([
            {
                status: StatusMode.VISIBLE,
                page: 1,
                page_size: 6,
                random_mode: true,
                start_time: 1_700_000_000_000,
            },
        ]);
    });

    it('带一个绿色标题，也是 V1 卡片', async () => {
        const it_ = library([photo('a'), photo('b')]);

        const card = json(await newPhotoCard(it_.photos, 1_700_000_000_000, false));

        expect(card.schema).toBe('1.0');
        expect(card.header).toEqual({
            title: { tag: 'plain_text', content: '今日新图' },
            template: 'green',
        });
    });

    // 「换一批」带回的是**时间下界**，不是标签 —— 换出来的必须还是那一天的新图。
    it('按钮带回时间下界与这一批的地址', async () => {
        const it_ = library([photo('a'), photo('b')]);

        const values = buttonValues(await newPhotoCard(it_.photos, 1_700_000_000_000, false));

        expect(values).toEqual([
            { type: UPDATE_DAILY_PHOTO_CARD, start_time: 1_700_000_000_000 },
            { type: FETCH_PHOTO_DETAILS, images: ['a.png', 'b.png'] },
        ]);
    });

    it('一张都没搜到就抛', async () => {
        const it_ = library([]);

        expect(newPhotoCard(it_.photos, 1_700_000_000_000, true)).rejects.toThrow('没有找到图片');
    });

    it('开了 allow_send_limit_photo 才放宽', async () => {
        const it_ = library([photo('a'), photo('b')]);

        await newPhotoCard(it_.photos, 1_700_000_000_000, true);

        expect(it_.asked[0]!.status).toBe(StatusMode.NOT_DELETE);
    });
});

describe('回调契约', () => {
    // 卡片留在飞书的聊天记录里，昨天发的明天还会被点。改这三个字面量 = 所有存量卡片
    // 的按钮变哑巴（回调进来落进 unknown 分支，没有任何反应）。
    it('三个 action type 是写死的线上字面量', () => {
        expect(UPDATE_PHOTO_CARD).toBe('update-photo-card');
        expect(FETCH_PHOTO_DETAILS).toBe('fetch-photo-details');
        expect(UPDATE_DAILY_PHOTO_CARD).toBe('update-daily-photo-card');
    });
});
