// 「水群」/「水群趋势」：一张七天周报卡片。
//
// 这张卡片有四块，每块的口径都是"错了照样画得出来、只是数字不对"，所以逐块钉：
//
//   龙王榜      本周条数排名前十，带上跟上周比的升降；**分数是本周的条数，不是两周合计**
//   活跃大盘    T-6 到 T-0 共七天，每天两条线（活跃人数 / 消息数），**没人说话的那天补 0**
//   分时段      T-7 到 T-1（**不含今天** —— 今天还没过完，画进去会是一条假的低谷）
//   词云        本周发言，去表情标记、去空串、去带链接的
//
// 另外两件事：机器人的消息全程不算（否则龙王榜第一名是赤尾自己），以及**没有 try/catch**
// —— 任何一步失败都让引擎收敛成 handler_error，与拆分前一致。

import { describe, expect, it } from 'bun:test';
import { runRulesWith } from '@inner/shared/rules';

import type { LarkBotLookup } from '../message/mentions';
import { readLarkMessageEvent } from '../message/read-message-event';
import type { LarkMessageEvent } from '../message/wire';
import type { LarkRecordedInbound } from '../projection/inbound-projection';
import { larkCommandContext } from '../rules/command-context';
import type { LarkCommandDeps } from '../rules/commands';
import { larkRuleMessage } from '../rules/rule-message';
import type { LarkHistoryMessage } from './chat-history';
import { historyCardCommand, larkHistoryCard } from './history-card';
import type { LarkExtractedKeywords } from './word-cloud';

/** 冻住的"现在"：2026-08-12（周三）15:00 本地时间。 */
const NOW = new Date(2026, 7, 12, 15, 0, 0);

/** 距今 daysAgo 天、当天 hour 点的一条消息。 */
function said(
    senderId: string,
    daysAgo: number,
    hour: number,
    text = '刻晴好看',
    over: Partial<LarkHistoryMessage> = {},
): LarkHistoryMessage {
    const at = new Date(NOW);
    at.setDate(at.getDate() - daysAgo);
    at.setHours(hour, 0, 0, 0);
    return {
        messageId: `om_${senderId}_${daysAgo}_${hour}`,
        senderId,
        isRobot: false,
        createTime: String(at.getTime()),
        text,
        ...over,
    };
}

function json(card: object): Record<string, any> {
    return JSON.parse(JSON.stringify(card));
}

function cardOf(
    messages: LarkHistoryMessage[],
    keywords: (texts: string[]) => LarkExtractedKeywords[] = () => [],
) {
    const asked: { chatId: string; start: number; end: number }[] = [];
    const segmented: string[][] = [];
    const build = larkHistoryCard({
        history: async (chatId, start, end) => {
            asked.push({ chatId, start, end });
            return messages;
        },
        keywords: async (texts) => {
            segmented.push(texts);
            return keywords(texts);
        },
        now: () => NOW,
    });
    return { asked, segmented, build };
}

// ---------------------------------------------------------------------------

describe('取的是哪一段历史', () => {
    it('十三天前到今天，一次', async () => {
        const { asked, build } = cardOf([]);

        await build('oc_1');

        expect(asked).toEqual([{ chatId: 'oc_1', start: 13, end: 0 }]);
    });
});

describe('龙王榜', () => {
    it('按本周条数排名，分数是本周条数', async () => {
        const { build } = cardOf([
            said('ou_a', 1, 10),
            said('ou_a', 2, 10),
            said('ou_a', 3, 10),
            said('ou_b', 1, 11),
            said('ou_b', 2, 11),
            said('ou_c', 1, 12),
        ]);

        const table = json(await build('oc_1')).body.elements[1];

        expect(table.rows.map((row: any) => [row.atUser, row.score])).toEqual([
            ['<at id=ou_a></at>', '3'],
            ['<at id=ou_b></at>', '2'],
            ['<at id=ou_c></at>', '1'],
        ]);
    });

    it('名次前三带奖牌，第四名起没有（那一格结尾留着上游的空格）', async () => {
        const { build } = cardOf(
            ['a', 'b', 'c', 'd'].flatMap((who, at) =>
                // 让 a 说 4 条、b 3 条、c 2 条、d 1 条。
                Array.from({ length: 4 - at }, (_, n) => said(`ou_${who}`, 1, n)),
            ),
        );

        const table = json(await build('oc_1')).body.elements[1];

        expect(table.rows.map((row: any) => row.orderText)).toEqual([
            '第一名 🥇',
            '第二名 🥈',
            '第三名 🥉',
            '第四名 ',
        ]);
    });

    it('最多十个', async () => {
        const { build } = cardOf(
            Array.from({ length: 15 }, (_, at) => said(`ou_${at}`, 1, at % 24)),
        );

        expect(json(await build('oc_1')).body.elements[1].rows).toHaveLength(10);
    });

    // 升降是「上周名次 - 本周名次」：上周第 3 本周第 1 就是 ↑2。上周没说过话是「新上榜」。
    it('跟上周比的升降', async () => {
        const { build } = cardOf([
            // 上周（T-13..T-7）：b 两条、a 一条 → b 第 0 名、a 第 1 名。
            said('ou_b', 8, 10),
            said('ou_b', 9, 10),
            said('ou_a', 8, 10),
            // 本周（T-6..T-0）：a 三条、b 两条、c 一条 → a 第 0、b 第 1、c 第 2。
            said('ou_a', 1, 10),
            said('ou_a', 2, 10),
            said('ou_a', 3, 10),
            said('ou_b', 1, 11),
            said('ou_b', 2, 11),
            said('ou_c', 1, 12),
        ]);

        const table = json(await build('oc_1')).body.elements[1];

        expect(table.rows.map((row: any) => [row.atUser, row.rankChange])).toEqual([
            ['<at id=ou_a></at>', '↑1'],
            ['<at id=ou_b></at>', '↓1'],
            ['<at id=ou_c></at>', '新上榜'],
        ]);
    });

    it('上下周名次一样是 -', async () => {
        const { build } = cardOf([said('ou_a', 8, 10), said('ou_a', 1, 10)]);

        expect(json(await build('oc_1')).body.elements[1].rows[0].rankChange).toBe('-');
    });

    // 上周那个窗口是 T-13..T-7，跟本周（T-6..T-0）严格不重叠。两头各钉一次：
    // 结束边界松一天，只在 T-6 说过话的人会被算成"上周也在榜上"；开始边界紧一天，
    // T-13 那天的发言会消失、老人变成新上榜。两种都只是升降那一列变了，没人对得上。
    it('上周窗口的两头', async () => {
        const onlyOnT6 = cardOf([said('ou_a', 6, 10), said('ou_a', 6, 11)]);
        expect(json(await onlyOnT6.build('oc_1')).body.elements[1].rows[0].rankChange).toBe(
            '新上榜',
        );

        const spokeOnT13 = cardOf([said('ou_b', 13, 10), said('ou_b', 1, 10)]);
        expect(json(await spokeOnT13.build('oc_1')).body.elements[1].rows[0].rankChange).toBe('-');
    });

    it('机器人不上榜', async () => {
        const { build } = cardOf([
            said('cli_bot', 1, 10, '好呀', { isRobot: true }),
            said('ou_a', 1, 11),
        ]);

        const table = json(await build('oc_1')).body.elements[1];

        expect(table.rows.map((row: any) => row.atUser)).toEqual(['<at id=ou_a></at>']);
    });

    it('本周没人说话时榜是空的，卡片照出', async () => {
        const { build } = cardOf([said('ou_a', 9, 10)]);

        const card = json(await build('oc_1'));

        expect(card.body.elements[1].rows).toEqual([]);
        expect(card.header.title.content).toBe('七天水群报告');
    });
});

describe('活跃大盘', () => {
    it('七天，每天两条线；没人说话的那天补 0', async () => {
        const { build } = cardOf([said('ou_a', 0, 10), said('ou_b', 0, 11)]);

        const values = json(await build('oc_1')).body.elements[2].chart_spec.data.values;

        expect(values).toHaveLength(14);
        // 最后一天是今天（08-12），两个人各说一条。
        expect(values.slice(-2)).toEqual([
            { x: '08-12', y: 2, series: '活跃人数' },
            { x: '08-12', y: 2, series: '消息数' },
        ]);
        // 第一天是 T-6（08-06），没人说话。
        expect(values.slice(0, 2)).toEqual([
            { x: '08-06', y: 0, series: '活跃人数' },
            { x: '08-06', y: 0, series: '消息数' },
        ]);
    });

    it('活跃人数按人去重，消息数不去重', async () => {
        const { build } = cardOf([said('ou_a', 0, 10), said('ou_a', 0, 11), said('ou_b', 0, 12)]);

        const values = json(await build('oc_1')).body.elements[2].chart_spec.data.values;

        expect(values.slice(-2)).toEqual([
            { x: '08-12', y: 2, series: '活跃人数' },
            { x: '08-12', y: 3, series: '消息数' },
        ]);
    });
});

describe('分时段活跃', () => {
    // T-7 到 T-1，**不含今天** —— 今天还没过完，画进去晚上那几个小时会是一条假的低谷。
    // 这个固定装置刻意让两个候选窗口给出**不同**的数字：今天两条、T-1 一条、T-7 一条，
    // 全在 03 点。正确的窗口（T-7..T-1）数出 2；错成本周那个窗口（T-6..T-0）会数出 3。
    it('二十四个小时；今天的不算，T-7 那天的算', async () => {
        const { build } = cardOf([
            said('ou_a', 0, 3),
            said('ou_b', 0, 3),
            said('ou_a', 1, 3),
            said('ou_a', 7, 3),
        ]);

        const values = json(await build('oc_1')).body.elements[3].chart_spec.data.values;

        expect(values).toHaveLength(24);
        expect(values[3]).toEqual({ x: '03', y: 2, series: '消息数' });
        expect(values[0]).toEqual({ x: '00', y: 0, series: '消息数' });
    });
});

describe('词云', () => {
    it('本周发言喂给分词，按权重降序进图', async () => {
        const { segmented, build } = cardOf(
            [said('ou_a', 1, 10, '刻晴'), said('ou_b', 8, 10, '上周说的')],
            (texts) =>
                texts.map((text) => ({
                    text,
                    keywords: [
                        { word: '刻晴', weight: 1 },
                        { word: '原神', weight: 3 },
                    ],
                })),
        );

        const values = json(await build('oc_1')).body.elements[4].chart_spec.data.values;

        // 只有本周那一条进了分词。
        expect(segmented).toEqual([['刻晴']]);
        expect(values).toEqual([
            { name: '原神', value: 0.75 },
            { name: '刻晴', value: 0.25 },
        ]);
    });

    it('空正文和带链接的不喂给分词', async () => {
        const { segmented, build } = cardOf([
            said('ou_a', 1, 10, ''),
            said('ou_a', 1, 11, '看这个 https://pixiv.net/x'),
            said('ou_a', 1, 12, '刻晴'),
        ]);

        await build('oc_1');

        expect(segmented).toEqual([['刻晴']]);
    });

    it('最多一百个词', async () => {
        const { build } = cardOf([said('ou_a', 1, 10, '刻晴')], () => [
            {
                text: '刻晴',
                keywords: Array.from({ length: 150 }, (_, at) => ({
                    word: `w${at}`,
                    weight: at + 1,
                })),
            },
        ]);

        expect(json(await build('oc_1')).body.elements[4].chart_spec.data.values).toHaveLength(100);
    });
});

describe('卡片整体', () => {
    it('绿色标题，五块按固定顺序', async () => {
        const { build } = cardOf([]);

        const card = json(await build('oc_1'));

        expect(card.header.template).toBe('green');
        expect(card.body.elements.map((e: any) => e.tag)).toEqual([
            'interactive_container',
            'table',
            'chart',
            'chart',
            'chart',
        ]);
        expect(card.body.elements[0].elements[0].content).toBe('龙王榜🐲');
        expect(card.body.elements[1].columns.map((c: any) => c.display_name)).toEqual([
            '名次',
            '龙王',
            '活跃分',
            '排名变化',
        ]);
    });
});

// ---------------------------------------------------------------------------
// 指令那一层
// ---------------------------------------------------------------------------

const APP_ID = 'cli_tool';
const BOT_NAME = 'tool';
const BOT_COMMON_USER_ID = 'cu_bot_tool';

const bots: LarkBotLookup = {
    byAppId: (appId) =>
        appId === APP_ID
            ? { botName: BOT_NAME, displayName: null, commonUserId: BOT_COMMON_USER_ID }
            : null,
    byUnionId: () => null,
};

function commandRig(
    options: {
        text?: string;
        mentionsBot?: boolean;
        messageType?: string;
        historyFails?: boolean;
    } = {},
) {
    const sent: { messageId: string; card: object; inThread: boolean }[] = [];
    const mentionsBot = options.mentionsBot ?? true;

    const deps = {
        api: {
            listMessages: async () => {
                if (options.historyFails) throw new Error('lark api 500');
                return { items: [], hasMore: false };
            },
            replyCard: async (messageId: string, card: object, inThread: boolean) => {
                sent.push({ messageId, card, inThread });
                return {};
            },
        },
        keywords: async () => [],
    } as unknown as LarkCommandDeps;

    const text = options.text ?? '水群';
    const messageType = options.messageType ?? 'text';
    const event: LarkMessageEvent = {
        app_id: APP_ID,
        sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
        message: {
            message_id: 'om_1',
            chat_id: 'oc_1',
            chat_type: 'group',
            create_time: '1700000000000',
            message_type: messageType,
            content:
                messageType === 'text'
                    ? JSON.stringify({ text: mentionsBot ? `@_user_1 ${text}` : text })
                    : JSON.stringify({ image_key: 'img_1' }),
            mentions: mentionsBot
                ? [
                      {
                          key: '@_user_1',
                          id: { union_id: 'on_bot_tool' },
                          name: 'tool-raw',
                          mentioned_type: 'bot',
                          bot_info: { app_id: APP_ID },
                      },
                  ]
                : [],
        },
    };

    const reading = readLarkMessageEvent(event, bots);
    if (!reading) throw new Error('fixture is not a message event');

    const recorded: LarkRecordedInbound = {
        projection: {
            commonUserId: 'cu_sender',
            commonConversationId: 'cc_1',
            commonMessageId: 'cm_1',
            commonRootMessageId: 'cm_root',
            commonReplyMessageId: undefined,
            mentionedCommonUserIds: mentionsBot ? [BOT_COMMON_USER_ID] : [],
        },
        commands: { appId: APP_ID, isAdmin: false, permission: {}, groupChat: null },
    };

    const context = larkCommandContext(reading, recorded, BOT_NAME);
    const message = larkRuleMessage(reading, recorded.projection, {
        botName: BOT_NAME,
        commonUserId: BOT_COMMON_USER_ID,
    });

    return {
        sent,
        run: () =>
            runRulesWith(message, {
                chatRules: [historyCardCommand(deps)(context)],
                botRole: 'utility',
                notBlocked: async () => true,
            }),
    };
}

describe('「水群」这条指令', () => {
    it('两个说法都命中，卡片挂在触发的那条消息上', async () => {
        for (const text of ['水群', '水群趋势']) {
            const { sent, run } = commandRig({ text });

            const terminal = await run();

            expect(terminal.kind).toBe('responded');
            expect(terminal.matchedRule).toBe('生成水群历史卡片');
            expect(sent).toHaveLength(1);
            expect(sent[0]!.messageId).toBe('om_1');
            // 拆分前 replyCard 没传 replyInThread，即不进话题。
            expect(sent[0]!.inThread).toBe(false);
        }
    });

    it('整句相等，不是包含', async () => {
        expect((await commandRig({ text: '水群报告' }).run()).kind).toBe('no_match');
        expect((await commandRig({ text: '看看水群' }).run()).kind).toBe('no_match');
    });

    it('群里没 @ 到我不命中', async () => {
        expect((await commandRig({ mentionsBot: false }).run()).kind).toBe('no_match');
    });

    it('非纯文本消息不命中', async () => {
        expect((await commandRig({ messageType: 'image' }).run()).kind).toBe('no_match');
    });

    // 拆分前这条指令没有 try/catch，失败一路交给引擎。照搬：不要顺手补一句道歉，
    // 那会把一个"报告没出来"的静默失败变成"报告出不来但用户以为出来了"。
    it('取历史失败时收敛成 handler_error，什么也不发', async () => {
        const { sent, run } = commandRig({ historyFails: true });

        const terminal = await run();

        expect(terminal.kind).toBe('handler_error');
        expect(sent).toEqual([]);
    });
});
