// 取一个群过去 N 天的历史消息。
//
// 三件事必须逐字保住，因为它们错了都不会报错：
//
//   * **切片数是 10。** 飞书那个接口按时间窗查，一个两周的窗口分十片并发拉。改这个数
//     直接改的是打飞书的并发度和额度消耗。
//   * **两个限速器真的在路径上。** 飞书给这个接口的额度是 40/s + 800/min；十片并发翻页
//     很容易撞上，超了返回的是错误而不是等待。
//   * **翻页翻到 has_more 为假为止。** 少翻一页只是数据变少 —— 报告照样出，没人对得上。

import { describe, expect, it } from 'bun:test';

import type { LarkMessageInfo, LarkMessagePage, LarkMessageQuery } from '../outbound/lark-api';
import {
    HISTORY_SPLIT_SIZE,
    larkChatHistory,
    splitTime,
    type LarkHistoryMessage,
} from './chat-history';

function page(items: LarkMessageInfo[], pageToken?: string): LarkMessagePage {
    return { items, hasMore: pageToken !== undefined, pageToken };
}

function raw(over: Partial<LarkMessageInfo> = {}): LarkMessageInfo {
    return {
        messageId: 'om_1',
        chatId: 'oc_1',
        senderId: 'ou_someone',
        senderIdType: 'open_id',
        messageType: 'text',
        createTime: '1700000000000',
        content: JSON.stringify({ text: '刻晴好看' }),
        mentions: [],
        ...over,
    };
}

/** 冻住的"现在"：2026-08-12 09:00 本地时间。 */
const NOW = new Date(2026, 7, 12, 9, 0, 0);

function rig(answer: (query: LarkMessageQuery, at: number) => LarkMessagePage) {
    const asked: LarkMessageQuery[] = [];
    const slept: number[] = [];
    let clockNow = 0;

    const history = larkChatHistory({
        api: {
            listMessages: async (query) => {
                asked.push(query);
                return answer(query, asked.length);
            },
        },
        now: () => NOW,
        clock: {
            now: () => clockNow,
            sleep: async (ms) => {
                slept.push(ms);
                clockNow += ms;
            },
        },
    });

    return { asked, slept, history };
}

describe('时间切片', () => {
    it('切成 splitSize 段，首尾相接、最后一段覆盖到 end', () => {
        expect(splitTime(0, 100, 4)).toEqual([
            [0, 24],
            [25, 49],
            [50, 74],
            [75, 100],
        ]);
    });

    it('切片数是 10（这是打飞书的并发度）', () => {
        expect(HISTORY_SPLIT_SIZE).toBe(10);
    });

    it('start >= end 或者 splitSize <= 0 直接抛', () => {
        expect(() => splitTime(10, 10, 4)).toThrow();
        expect(() => splitTime(10, 5, 4)).toThrow();
        expect(() => splitTime(0, 100, 0)).toThrow();
    });
});

describe('取历史', () => {
    it('十片并发，窗口是「N 天前的零点」到「M 天前的当天最后一刻」', async () => {
        const { asked, history } = rig(() => page([]));

        await history('oc_1', 13, 0);

        expect(asked).toHaveLength(HISTORY_SPLIT_SIZE);
        expect(asked.every((query) => query.chatId === 'oc_1')).toBe(true);

        // 十片拼起来就是整个窗口，中间没有缺口。
        const starts = asked.map((q) => q.startTime!).sort((a, b) => a - b);
        const ends = asked.map((q) => q.endTime!).sort((a, b) => a - b);
        expect(starts[0]).toBe(new Date(2026, 6, 30, 0, 0, 0).getTime() / 1000);
        expect(ends[ends.length - 1]).toBe(
            Math.floor(new Date(2026, 7, 12, 23, 59, 59, 999).getTime() / 1000),
        );
        for (let at = 1; at < starts.length; at++) {
            expect(starts[at]).toBe(ends[at - 1]! + 1);
        }
    });

    it('翻页翻到 has_more 为假为止，游标带上一页给的 token', async () => {
        const { asked, history } = rig((query) => {
            if (query.startTime !== undefined && query.pageToken === undefined) {
                // 每一片的第一页都说还有下一页。
                return page([raw({ messageId: `om_${query.startTime}` })], 'tok');
            }
            return page([raw({ messageId: 'om_last' })]);
        });

        const messages = await history('oc_1', 13, 0);

        expect(asked).toHaveLength(HISTORY_SPLIT_SIZE * 2);
        expect(asked.filter((q) => q.pageToken === 'tok')).toHaveLength(HISTORY_SPLIT_SIZE);
        expect(messages).toHaveLength(HISTORY_SPLIT_SIZE * 2);
    });

    // 撤回过的消息仍然出现在历史里，靠 deleted 区分；合并转发是一整包别人的消息，
    // 拿它统计等于把别人群的发言算进本群。两条过滤与拆分前逐字相同。
    it('撤回过的和合并转发的都不算', async () => {
        const { history } = rig((_query, at) =>
            at === 1
                ? page([
                      raw({ messageId: 'om_alive' }),
                      raw({ messageId: 'om_deleted', deleted: true }),
                      raw({ messageId: 'om_forward', messageType: 'merge_forward' }),
                  ])
                : page([]),
        );

        const messages = await history('oc_1', 13, 0);

        expect(messages.map((m) => m.messageId)).toEqual(['om_alive']);
    });

    it('一片打不通就整个抛（水群报告宁可不发，也不发一份缺了十分之一的）', async () => {
        const history = larkChatHistory({
            api: {
                listMessages: async (query) => {
                    if (query.pageToken === undefined && query.startTime! % 2 === 1) {
                        throw new Error('lark api 500');
                    }
                    return page([]);
                },
            },
            now: () => NOW,
        });

        expect(history('oc_1', 13, 0)).rejects.toThrow();
    });
});

describe('历史消息读成什么', () => {
    async function readOne(over: Partial<LarkMessageInfo>): Promise<LarkHistoryMessage> {
        const { history } = rig((_query, at) => (at === 1 ? page([raw(over)]) : page([])));
        const messages = await history('oc_1', 13, 0);
        return messages[0]!;
    }

    it('真人的消息：sender.id 原样留着，isRobot 为假', async () => {
        expect(await readOne({ senderId: 'ou_a', senderIdType: 'open_id' })).toMatchObject({
            senderId: 'ou_a',
            isRobot: false,
            createTime: '1700000000000',
        });
    });

    // 飞书对 bot 发的历史消息给的 id_type 是 app_id。判据就是它 —— 认不出来的话
    // 龙王榜上会出现赤尾自己。
    it('bot 的消息：id_type 是 app_id 就算机器人', async () => {
        expect(await readOne({ senderId: 'cli_x', senderIdType: 'app_id' })).toMatchObject({
            senderId: 'cli_x',
            isRobot: true,
        });
    });

    it('没有发送者时记成 unknown（不是空串）', async () => {
        expect((await readOne({ senderId: undefined })).senderId).toBe('unknown');
    });

    // 词云吃的是去掉 `[表情]` / `<标签>` 之后的正文，与入站那条链同一套处理。
    it('正文去掉表情标记和 @ 占位符', async () => {
        expect(
            (await readOne({ content: JSON.stringify({ text: '@_user_1 [微笑] 刻晴  好看' }) }))
                .text,
        ).toBe(' 刻晴 好看');
    });

    it('正文不是 JSON、或者根本没有 text 字段时是空串', async () => {
        expect((await readOne({ content: 'not json at all' })).text).toBe('');
        expect((await readOne({ content: JSON.stringify({ image_key: 'i' }) })).text).toBe('');
        expect((await readOne({ content: undefined })).text).toBe('');
    });
});

describe('限速器真的在路径上', () => {
    // 飞书给这个接口的额度是 40/s。十片并发翻页很容易撞上，超了返回的是错误而不是等待 ——
    // 所以限速器少接一个，症状是「水群偶尔整个失败」而不是「慢一点」。
    it('一秒内超过 40 次请求时会等', async () => {
        // 每片翻五页 = 50 次请求，超过每秒 40 次的额度。
        const { slept, history } = rig((query) => {
            const at = Number(query.pageToken ?? '0');
            return at < 4 ? page([], String(at + 1)) : page([]);
        });

        await history('oc_1', 13, 0);

        expect(slept.length).toBeGreaterThan(0);
    });
});
