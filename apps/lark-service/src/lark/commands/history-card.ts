// 「水群」/「水群趋势」：一张七天周报卡片。
//
//     过去 14 天的群消息 ──剔机器人──┬──▶ 龙王榜（本周条数排名 + 跟上周比的升降）
//                                    ├──▶ 活跃大盘（T-6..T-0，每天两条线）
//                                    ├──▶ 分时段活跃（T-7..T-1，二十四小时）
//                                    └──▶ 词云（本周发言）
//
// ## 四块各自的时间窗不一样，这不是笔误
//
//   龙王榜的"本周"  T-6..T-0    含今天 —— 榜是给人看的，今天说的话得算
//   龙王榜的"上周"  T-13..T-7   跟本周严格不重叠
//   活跃大盘        T-6..T-0    与本周同窗
//   分时段          T-7..T-1    **不含今天** —— 今天还没过完，画进去晚上那几个
//                               小时会是一条假的低谷，看起来像群突然死了
//   词云            T-6..T-0    与本周同窗
//
// 全部照搬拆分前。改任何一个窗口都不会报错，只会让图变得不对而没人说得出哪里不对。
//
// ## 没有 try/catch
//
// 与拆分前一致：取历史或分词失败时整个 handler 抛出去，引擎收敛成 handler_error（有
// 日志、有终态）。不要顺手补一句"生成失败"的道歉 —— 那会多出一条上游没有的可观测行为。
//
// ## 那张表的 `<at id=…>` 用的是 open_id
//
// 历史接口给的 `sender.id` 对真人就是 open_id，而飞书卡片的 `<at>` 认它。所以龙王榜不
// 需要经过公共层身份 —— 也正因为如此，这张表里出现的是飞书原生 id，不是 common_user_id。

import dayjs from 'dayjs';
import _ from 'lodash';
import {
    BarChartSpec,
    CardHeader,
    ChartElement,
    InteractiveContainerComponent,
    LarkCard,
    LineChartSpec,
    MarkdownComponent,
    TableColumn,
    TableComponent,
    WordCloudChartSpec,
} from 'feishu-card';
import { EqualText, NeedRobotMention, TextMessageLimit } from '@inner/shared/rules';

import type { LarkCommand, LarkCommandDeps } from '../rules/commands';
import { larkChatHistory, type LarkChatHistory, type LarkHistoryMessage } from './chat-history';
import { buildWeeklyWordCloud, type LarkKeywordExtractor } from './word-cloud';

/** 榜上最多几个人。拆分前就是 10，也是那张表的分页大小。 */
const TOP_TALKERS = 10;

/** 词云最多几个词。拆分前就是 100。 */
const TOP_WORDS = 100;

/** 取多少天的历史。拆分前就是 13 天前到今天。 */
const HISTORY_FROM = 13;
const HISTORY_TO = 0;

export interface LarkHistoryCardDeps {
    history: LarkChatHistory;
    keywords: LarkKeywordExtractor;
    /** "现在"。四个时间窗全按自然日算，只有可控的时钟才测得出边界。 */
    now: () => Date;
}

/** 龙王榜那张表的一行。 */
interface TalkerRow {
    orderText: string;
    atUser: string;
    score: string;
    rankChange: string;
}

export function larkHistoryCard(
    deps: LarkHistoryCardDeps,
): (chatId: string) => Promise<LarkCard> {
    return async (chatId) => {
        // 剔掉机器人（否则龙王榜第一名是赤尾自己）。`!!senderId` 照搬上游那条 —— 取不到
        // 发送者时上面记的是 'unknown'，所以它实际只挡空串。
        const all = (await deps.history(chatId, HISTORY_FROM, HISTORY_TO)).filter(
            (message) => !message.isRobot && !!message.senderId,
        );
        const at = dayjs(deps.now());
        const thisWeek = withinDays(at, all, 6, 0);

        return new LarkCard()
            .withHeader(new CardHeader('七天水群报告').color('green'))
            .addElement(
                talkerTableTitle(),
                talkerTable(thisWeek, withinDays(at, all, 13, 7)),
                dailyChart(at, thisWeek),
                hourlyChart(withinDays(at, all, 7, 1)),
                await wordCloudChart(deps.keywords, thisWeek),
            );
    };
}

// ---------------------------------------------------------------------------
// 时间窗与分组
// ---------------------------------------------------------------------------

/**
 * `[from 天前的零点, to 天前当天的最后一刻]` 之间的消息。
 *
 * **严格开区间**（isAfter / isBefore），照搬上游。读不出时间戳的消息（createTime 缺失或
 * 不是数）在这里被静默排除 —— `dayjs(NaN)` 对两边都返回 false。
 */
function withinDays(
    now: dayjs.Dayjs,
    messages: LarkHistoryMessage[],
    from: number,
    to: number,
): LarkHistoryMessage[] {
    const start = now.startOf('day').subtract(from, 'day');
    const end = now.endOf('day').subtract(to, 'day');
    return messages.filter((message) => {
        const said = dayjs(parseInt(message.createTime ?? ''));
        return said.isAfter(start) && said.isBefore(end);
    });
}

function messageStatistic(messages: LarkHistoryMessage[]): {
    messageCount: number;
    messagePersonCount: number;
} {
    return {
        messageCount: messages.length,
        messagePersonCount: new Set(messages.map((message) => message.senderId)).size,
    };
}

/** 每个人说了几条。 */
function countByPerson(messages: LarkHistoryMessage[]): Record<string, number> {
    const counts: Record<string, number> = {};
    for (const message of messages) {
        counts[message.senderId] = (counts[message.senderId] ?? 0) + 1;
    }
    return counts;
}

/** 条数降序之后每个人的名次（0 开始）。 */
function ranking(messages: LarkHistoryMessage[]): Map<string, { count: number; rank: number }> {
    return new Map(
        Object.entries(countByPerson(messages))
            .sort((a, b) => b[1] - a[1])
            .map(([senderId, count], rank) => [senderId, { count, rank }]),
    );
}

// ---------------------------------------------------------------------------
// 龙王榜
// ---------------------------------------------------------------------------

function talkerTableTitle(): InteractiveContainerComponent {
    return new InteractiveContainerComponent()
        .pushElement(new MarkdownComponent('龙王榜🐲').setTextAlign('center'))
        .setMargin('0 2px')
        .setPadding('4px 8px 4px 8px')
        .setBackgroundStyle('green-100')
        .setBorderColor('green-400')
        .setHasBorder(true)
        .setCornerRadius('8px');
}

/**
 * 名次那一格。
 *
 * 第四名起结尾**留着一个空格**（上游的模板就是 `第X名 ${奖牌或空串}`）。看着像笔误，
 * 但它已经在群里出现过无数次了，改掉是一次没人要求过的文案变更。
 */
function rankText(rank: number): string {
    const medals = ['🥇', '🥈', '🥉'];
    const numbers = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
    return `第${numbers[rank]}名 ${rank < 3 ? medals[rank] : ''}`;
}

/** 升降 = 上周名次 - 本周名次。上周没说过话是「新上榜」。 */
function rankChange(lastWeekRank: number | undefined, thisWeekRank: number): string {
    if (lastWeekRank === undefined) return '新上榜';
    const diff = lastWeekRank - thisWeekRank;
    if (diff > 0) return `↑${diff}`;
    if (diff < 0) return `↓${-diff}`;
    return '-';
}

function talkerTable(
    thisWeek: LarkHistoryMessage[],
    lastWeek: LarkHistoryMessage[],
): TableComponent<TalkerRow> {
    const lastWeekRanks = ranking(lastWeek);
    // **只保留本周说过话的人**，分数也只算本周 —— 两周合计会让一个上周刷屏、本周
    // 沉默的人挂在榜首。
    const rows = [...ranking(thisWeek).entries()]
        .sort((a, b) => a[1].rank - b[1].rank)
        .slice(0, TOP_TALKERS)
        .map(([senderId, { count, rank }]) => ({
            orderText: rankText(rank),
            atUser: `<at id=${senderId}></at>`,
            score: count.toString(),
            rankChange: rankChange(lastWeekRanks.get(senderId)?.rank, rank),
        }));

    const table = new TableComponent<TalkerRow>().setPageSize(TOP_TALKERS);
    table.addColumn(TableColumn.markdown('orderText').setDisplayName('名次'));
    table.addColumn(TableColumn.markdown('atUser').setDisplayName('龙王').setWidth('35%'));
    table.addColumn(TableColumn.markdown('score').setDisplayName('活跃分'));
    table.addColumn(TableColumn.text('rankChange').setDisplayName('排名变化'));
    table.appendRows(...rows);
    return table;
}

// ---------------------------------------------------------------------------
// 两张时间图
// ---------------------------------------------------------------------------

/**
 * 活跃大盘：T-6..T-0 每天两条线。
 *
 * **没人说话的那天补 0**：只画有数据的天会让 x 轴的间距说谎（三天没人说话看起来像
 * 连着的三天）。
 */
function dailyChart(now: dayjs.Dayjs, thisWeek: LarkHistoryMessage[]): ChartElement<LineChartSpec> {
    const spec = new LineChartSpec(
        { text: '活跃大盘' },
        'x',
        'y',
        'series',
        { visible: true },
        { visible: true, orient: 'bottom', position: 'middle' },
        'monotone',
    );

    const byDate = _.groupBy(thisWeek, (message) =>
        dayjs(parseInt(message.createTime ?? '')).format('YYYY-MM-DD'),
    );
    for (let ago = 6; ago >= 0; ago--) {
        const day = now.subtract(ago, 'day');
        const { messagePersonCount, messageCount } = messageStatistic(
            byDate[day.format('YYYY-MM-DD')] ?? [],
        );
        spec.addLineData(day.format('MM-DD'), messagePersonCount, '活跃人数');
        spec.addLineData(day.format('MM-DD'), messageCount, '消息数');
    }

    return new ChartElement(spec);
}

/** 分时段活跃：二十四个小时各一根柱子，同样补 0。 */
function hourlyChart(lastSevenDays: LarkHistoryMessage[]): ChartElement<BarChartSpec> {
    const spec = new BarChartSpec({ text: '分时段活跃情况' }, 'x', 'y', 'series', {
        visible: true,
    });

    const byHour = _.groupBy(lastSevenDays, (message) =>
        dayjs(parseInt(message.createTime ?? '')).format('HH'),
    );
    for (let hour = 0; hour < 24; hour++) {
        const key = String(hour).padStart(2, '0');
        spec.addLineData(key, messageStatistic(byHour[key] ?? []).messageCount, '消息数');
    }

    return new ChartElement(spec);
}

// ---------------------------------------------------------------------------
// 词云
// ---------------------------------------------------------------------------

/**
 * 喂给分词服务的是**本周发言里去掉表情标记之后还剩字、且不含链接的**那些。
 *
 * 链接那条筛选是有理由的：一条 pixiv 地址分词之后会炸出一堆域名碎片，而它们在群里
 * 出现的频率极高，不挡的话整张词云会被 `pixiv` `net` 这类词占满。
 */
async function wordCloudChart(
    keywords: LarkKeywordExtractor,
    thisWeek: LarkHistoryMessage[],
): Promise<ChartElement<WordCloudChartSpec>> {
    const texts = thisWeek
        .map((message) => message.text)
        .filter((text) => text.length > 0 && !text.includes('https://'));

    const cloud = await buildWeeklyWordCloud(keywords, texts);
    const spec = new WordCloudChartSpec({ text: '本群词云' }, 'name', 'value', 'name');
    for (const [word, weight] of [...cloud.entries()]
        .sort((a, b) => b[1] - a[1])
        .slice(0, TOP_WORDS)) {
        spec.addWordCloudData(word, weight);
    }

    return new ChartElement(spec);
}

// ---------------------------------------------------------------------------
// 指令
// ---------------------------------------------------------------------------

export function historyCardCommand(deps: LarkCommandDeps): LarkCommand {
    // **装配期建一次**：飞书那两个限速器住在 larkChatHistory 里，按应用共享额度，
    // 逐消息重建等于把额度乘以敲指令的次数（见 chat-history.ts）。
    const card = larkHistoryCard({
        history: larkChatHistory({ api: deps.api, now: () => new Date() }),
        keywords: deps.keywords,
        now: () => new Date(),
    });

    return (context) => ({
        rules: [EqualText('水群', '水群趋势'), TextMessageLimit, NeedRobotMention],
        comment: '生成水群历史卡片',
        category: 'utility',
        handler: async () => {
            // 没有 try/catch，理由见文件头。拆分前 replyCard 没传 replyInThread。
            await deps.api.replyCard(
                context.message.messageId,
                await card(context.message.chatId),
                false,
            );
        },
    });
}
