// 两个图片日报。
//
// 它们往**写死的真实飞书群**发消息，所以群 id 是这批代码里最不能错的三个字面量 ——
// 错了不会报错，只会发到别的群里去。lane gate（非 prod 部署不挂任务）在 schedule.ts
// 那一层，这里只管任务本体。

import { describe, expect, it } from 'bun:test';
import type { ImageForLark } from '@inner/pixiv-client';

import {
    DAILY_NEW_PHOTO_CHAT,
    DAILY_PHOTO_CHAT,
    DAILY_PHOTO_NUDGE_CHAT,
    dailyNewPhoto,
    dailyPhoto,
    type DailyPhotoDeps,
} from './daily';
import type { LarkReadyPhotos } from './ready';

function photo(key: string): ImageForLark {
    return { pixiv_addr: `${key}.png`, image_key: key, width: 100, height: 100 };
}

function rig(
    options: { photos?: LarkReadyPhotos; sentMessageId?: string | undefined; now?: Date } = {},
) {
    const sentCards: { chatId: string; card: any }[] = [];
    const sentText: { chatId: string; text: string }[] = [];
    const replied: { messageId: string; card: any; inThread: boolean }[] = [];
    const asked: any[] = [];
    const waited: number[] = [];

    const deps: DailyPhotoDeps = {
        api: {
            sendCard: async (chatId, card) => {
                sentCards.push({ chatId, card });
                return {};
            },
            sendText: async (chatId, text) => {
                sentText.push({ chatId, text });
                return { messageId: 'sentMessageId' in options ? options.sentMessageId : 'om_sent' };
            },
            replyCard: async (messageId, card, inThread) => {
                replied.push({ messageId, card, inThread });
                return {};
            },
        },
        photos:
            options.photos ??
            (async (query) => {
                asked.push(query);
                return [photo('a'), photo('b')];
            }),
        wait: async (ms) => void waited.push(ms),
        now: () => options.now ?? new Date('2026-08-12T10:00:00.000Z'),
    };

    return { deps, sentCards, sentText, replied, asked, waited };
}

function json(card: object): Record<string, any> {
    return JSON.parse(JSON.stringify(card));
}

// ---------------------------------------------------------------------------

describe('每日一图（18:00）', () => {
    it('随机取一张可见图', async () => {
        const it_ = rig();

        await dailyPhoto(it_.deps)();

        expect(it_.asked).toEqual([
            { status: 1, page: 1, page_size: 1, random_mode: true },
        ]);
    });

    it('发一张带蓝色标题的单图卡片给订阅群', async () => {
        const it_ = rig();

        await dailyPhoto(it_.deps)();

        expect(it_.sentCards[0]!.chatId).toBe(DAILY_PHOTO_CHAT);
        const card = json(it_.sentCards[0]!.card);
        expect(card.header).toEqual({
            title: { tag: 'plain_text', content: '今天的每日一图' },
            template: 'blue',
        });
        expect(card.body.elements[0]).toMatchObject({ tag: 'img', img_key: 'a' });
    });

    // 第二个群的玩法是：先发一句"每日一图"，等十秒，再把卡片挂在那句上回复（进话题）。
    // 顺序和那次等待都是拆分前的形态，照搬。
    it('隔十秒再往另一个群补一条，卡片挂在那句话上、进话题', async () => {
        const it_ = rig();

        await dailyPhoto(it_.deps)();

        expect(it_.sentText).toEqual([{ chatId: DAILY_PHOTO_NUDGE_CHAT, text: '每日一图' }]);
        expect(it_.waited).toEqual([10_000]);
        expect(it_.replied).toHaveLength(1);
        expect(it_.replied[0]!.messageId).toBe('om_sent');
        expect(it_.replied[0]!.inThread).toBe(true);
        // 两个群看到的是同一张卡片。
        expect(json(it_.replied[0]!.card)).toEqual(json(it_.sentCards[0]!.card));
    });

    it('两个群的 id 不一样（发重了没人会发现）', () => {
        expect(DAILY_PHOTO_CHAT).not.toBe(DAILY_PHOTO_NUDGE_CHAT);
    });

    // 一张都没有就整个跳过 —— 不发空卡片，也不发那句"每日一图"。
    it('一张图都没有时什么都不发', async () => {
        const it_ = rig({ photos: async () => [] });

        await dailyPhoto(it_.deps)();

        expect(it_.sentCards).toEqual([]);
        expect(it_.sentText).toEqual([]);
        expect(it_.waited).toEqual([]);
    });

    // 定时任务的基座会接住它并记一条日志（见 schedule.ts 的 fire）。这里往上抛是
    // 对的：吞掉的话"日报没发出去"完全不可观测。
    it('发不出去就往上抛，交给定时任务基座记账', async () => {
        const it_ = rig();
        it_.deps.api.sendCard = async () => {
            throw new Error('lark is down');
        };

        expect(dailyPhoto(it_.deps)()).rejects.toThrow('lark is down');
    });

    it('平台没回 message_id 时说清楚，不拿 undefined 去打飞书', async () => {
        const it_ = rig({ sentMessageId: undefined });

        expect(dailyPhoto(it_.deps)()).rejects.toThrow(/message id/i);
        // 第一个群那条已经发出去了，不会因此回滚。
        expect(it_.sentCards).toHaveLength(1);
    });
});

describe('今日新图（19:30）', () => {
    it('取昨天这个时刻之后入库的图', async () => {
        const it_ = rig({ now: new Date('2026-08-12T19:30:00.000Z') });

        await dailyNewPhoto(it_.deps)();

        expect(it_.asked).toEqual([
            expect.objectContaining({
                start_time: new Date('2026-08-11T19:30:00.000Z').valueOf(),
                random_mode: true,
                page_size: 6,
            }),
        ]);
    });

    // 定时任务这条路没有会话开关可读（它不是被谁触发的），所以永远按"仅可见"取。
    it('不放宽到"仅自己可见"的那些图', async () => {
        const it_ = rig();

        await dailyNewPhoto(it_.deps)();

        expect(it_.asked[0]!.status).toBe(1);
    });

    it('发给第三个群', async () => {
        const it_ = rig();

        await dailyNewPhoto(it_.deps)();

        expect(it_.sentCards).toHaveLength(1);
        expect(it_.sentCards[0]!.chatId).toBe(DAILY_NEW_PHOTO_CHAT);
        expect(json(it_.sentCards[0]!.card).header.title.content).toBe('今日新图');
    });

    it('三个群 id 互不相同', () => {
        expect(new Set([DAILY_PHOTO_CHAT, DAILY_PHOTO_NUDGE_CHAT, DAILY_NEW_PHOTO_CHAT]).size).toBe(
            3,
        );
    });

    // **与「每日一图」不同**：这一个自己吞错。拆分前就是这样（它整个包在 try 里），
    // 照搬 —— 于是它失败时基座那条 "finished" 日志照常打出来。
    it('自己吞错，不往上抛', async () => {
        const it_ = rig({ photos: async () => [] });

        await dailyNewPhoto(it_.deps)();

        expect(it_.sentCards).toEqual([]);
    });
});
