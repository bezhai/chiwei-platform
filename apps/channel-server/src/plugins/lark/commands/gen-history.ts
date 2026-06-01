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
import { Message } from 'core/models/message';
import { buildWeeklyWordCloud } from '@core/services/text-analytics/jieba';
import { replyCard, searchGroupMessage } from '@lark/basic/message';
import type { RuleMessage } from 'core/rules/rule-message';
import { larkContextStore } from '../lark-context-store';

// lark-only handler：入口适配 RuleMessage → 从 lark 私有 store 取回飞书 Message
// 跑不变的内部逻辑（水群报告深度依赖飞书群消息/卡片）；缺 store entry fail-loud。
export async function genHistoryCard(message: RuleMessage) {
    return genHistoryCardImpl(larkContextStore.get(message.commonMessageId));
}

function splitTime(start: number, end: number, splitSize: number): number[][] {
    // 确保输入有效
    if (splitSize <= 0 || start >= end) {
        throw new Error('Invalid input: splitSize must be > 0 and start must be < end');
    }

    const step = Math.floor((end - start) / splitSize); // 每个区间的步长
    const result: number[][] = [];

    for (let i = 0; i < splitSize - 1; i++) {
        // 创建每个子区间
        result.push([start + i * step, start + (i + 1) * step - 1]);
    }

    // 添加最后一个区间，确保覆盖到 end
    result.push([start + (splitSize - 1) * step, end]);

    return result;
}

async function genHistoryCardImpl(message: Message) {
    const allMessages = humanMessageFilter(await getHistoryMessage(message.chatId, 13, 0));

    const activeChartSpec = new LineChartSpec(
        { text: '活跃大盘' },
        'x',
        'y',
        'series',
        {
            visible: true,
        },
        {
            visible: true,
            orient: 'bottom',
            position: 'middle',
        },
        'monotone',
    );
    const activeChart = new ChartElement(activeChartSpec);

    // 这里取T-6到T-0的消息
    const messagesGroupByDate = messageGroupByDate(messageTimeFilter(allMessages, 6, 0));

    // 生成最近7天的日期列表
    const dateKeys = Array.from({ length: 7 }, (_, i) =>
        dayjs()
            .subtract(6 - i, 'day')
            .format('YYYY-MM-DD'),
    );

    // 如果某天没有消息,messagesGroupByDate中就不会有这一天的key,但我们的dateKeys中会包含这一天
    dateKeys.forEach((date) => {
        const printDate = dayjs(date).format('MM-DD');
        const { messagePersonCount, messageCount } = messageStatistic(
            messagesGroupByDate[date] || [],
        );
        activeChartSpec.addLineData(printDate, messagePersonCount, '活跃人数');
        activeChartSpec.addLineData(printDate, messageCount, '消息数');
    });

    const hourActiveChartSpec = new BarChartSpec({ text: '分时段活跃情况' }, 'x', 'y', 'series', {
        visible: true,
    });
    const hourActiveChart = new ChartElement(hourActiveChartSpec);

    // 这里取T-7到T-1的消息
    const messagesGroupByHour = messageGroupByHour(messageTimeFilter(allMessages, 7, 1));

    const hourKeys = Array.from({ length: 24 }, (_, i) => dayjs().hour(i).format('HH'));

    // 如果某个小时没有消息,messagesGroupByHour中就不会有这个小时的key,但我们的hourKeys中会包含
    hourKeys.forEach((hour) => {
        const { messagePersonCount, messageCount } = messageStatistic(
            messagesGroupByHour[hour] || [],
        );
        // hourActiveChart.chart_spec.addLineData(hour, messagePersonCount, '活跃人数');
        hourActiveChartSpec.addLineData(hour, messageCount, '消息数');
    });

    // 需要对T-13到T-7的发言和T-6到T-0的发言人数进行聚合, 进行排序, 得到排名map
    const lastWeekRankMap = Object.entries(
        messageByPersonCount(messageTimeFilter(allMessages, 13, 7)),
    )
        .sort((a, b) => b[1] - a[1])
        .map(([openId, count], index) => ({
            openId,
            count,
            lastWeekRank: index,
        }));
    const thisWeekRankMap = Object.entries(
        messageByPersonCount(messageTimeFilter(allMessages, 6, 0)),
    )
        .sort((a, b) => b[1] - a[1])
        .map(([openId, count], index) => ({
            openId,
            count,
            thisWeekRank: index,
        }));
    // 只保留这周有发言的用户（以 thisWeekRankMap 为主，LEFT JOIN lastWeekRankMap）
    const rankMap: Record<
        string,
        { openId: string; count: number; lastWeekRank?: number; thisWeekRank: number }
    > = {};

    thisWeekRankMap.forEach((item) => {
        // 查找上周排名
        const last = lastWeekRankMap.find((x) => x.openId === item.openId);
        rankMap[item.openId] = {
            openId: item.openId,
            count: item.count, // 只用本周的
            thisWeekRank: item.thisWeekRank,
            lastWeekRank: last?.lastWeekRank,
        };
    });

    // 我们需要将rankMap转成数组进行排序, 按thisWeekRank排序, 取前10个
    const sortedRankMap = Object.values(rankMap)
        .sort((a, b) => a.thisWeekRank - b.thisWeekRank)
        .slice(0, 10);

    const personTableTitle = new InteractiveContainerComponent()
        .pushElement(new MarkdownComponent('龙王榜🐲').setTextAlign('center'))
        .setMargin('0 2px')
        .setPadding('4px 8px 4px 8px')
        .setBackgroundStyle('green-100')
        .setBorderColor('green-400')
        .setHasBorder(true)
        .setCornerRadius('8px');

    type PersonTableData = {
        orderText: string;
        atUser: string;
        score: string;
        rankChange: string;
    };
    const personTable = new TableComponent<PersonTableData>().setPageSize(10);
    personTable.addColumn(TableColumn.markdown('orderText').setDisplayName('名次'));
    personTable.addColumn(TableColumn.markdown('atUser').setDisplayName('龙王').setWidth('35%'));
    personTable.addColumn(TableColumn.markdown('score').setDisplayName('活跃分'));
    personTable.addColumn(TableColumn.text('rankChange').setDisplayName('排名变化'));

    sortedRankMap.forEach((item) => {
        let rankChange = '';
        if (item.lastWeekRank !== undefined && item.thisWeekRank !== undefined) {
            const diff = item.lastWeekRank - item.thisWeekRank;
            if (diff > 0) {
                rankChange = `↑${diff}`;
            } else if (diff < 0) {
                rankChange = `↓${-diff}`;
            } else {
                rankChange = '-';
            }
        } else if (item.lastWeekRank === undefined) {
            rankChange = '新上榜';
        } else {
            rankChange = '-';
        }
        personTable.appendRows({
            orderText: getRankText(item.thisWeekRank ?? 0),
            atUser: `<at id=${item.openId}></at>`,
            score: (item.count ?? 0).toString(),
            rankChange,
        });
    });

    // 我们还需要一个词云, 需要先换成去除emoji后的消息
    const clearTexts = messageTimeFilter(allMessages, 6, 0)
        .map((message) => message.withoutEmojiText())
        .filter((text) => text.length > 0 && !text.includes('https://'));

    const wordCloudMap = await buildWeeklyWordCloud(clearTexts);

    // 对 wordCloudMap 进行排序，按照权重降序排列, 取前100个
    const sortedWordCloudMap = Object.entries(wordCloudMap)
        .sort((a, b) => b[1] - a[1])
        .slice(0, 100);

    // 构建词云的图表
    const wordCloudChartSpec = new WordCloudChartSpec(
        { text: '本群词云' },
        'name',
        'value',
        'name',
    );
    const wordCloudChart = new ChartElement(wordCloudChartSpec);

    // 添加数据到词云
    sortedWordCloudMap.forEach(([name, value]) => {
        wordCloudChartSpec.addWordCloudData(name, value);
    });

    const card = new LarkCard().withHeader(new CardHeader('七天水群报告').color('green'));
    card.addElement(personTableTitle, personTable, activeChart, hourActiveChart, wordCloudChart);

    // 最后需要发送卡片
    replyCard(message.messageId, card);
}

function humanMessageFilter(messages: Message[]) {
    return messages.filter((message) => !message.isRobotMessage && !!message.sender);
}

function getRankText(index: number) {
    const rankEmojis = ['🥇', '🥈', '🥉'];
    const rankZhNumbers = ['一', '二', '三', '四', '五', '六', '七', '八', '九', '十'];
    return `第${rankZhNumbers[index]}名 ${index < 3 ? rankEmojis[index] : ''}`;
}

async function getHistoryMessage(
    chatId: string,
    startDateDiff: number = 0,
    endDateDiff: number = 0,
) {
    const startTime = dayjs().startOf('day').subtract(startDateDiff, 'day').unix();
    const endTime = dayjs().endOf('day').subtract(endDateDiff, 'day').unix();

    // 分割时间区间
    const splitSize = 10;
    const timeIntervals = splitTime(startTime, endTime, splitSize); // 返回 [[start1, end1], [start2, end2], ...]

    const messageList: Message[] = [];
    const promises: Promise<void>[] = [];

    for (const [start, end] of timeIntervals) {
        const task = async () => {
            const messages = await searchGroupMessage(chatId, start, end);
            if (messages && messages.length > 0) {
                messageList.push(...messages);
            }
        };

        promises.push(task());
    }

    try {
        await Promise.all(promises);
    } catch (error) {
        console.error('Error fetching messages:', error);
        throw error;
    }

    return messageList;
}

/**
 * 过滤消息时间
 * @param messages 消息列表
 * @param startDateDiff 开始日期偏移量
 * @param endDateDiff 结束日期偏移量
 * @returns 过滤后的消息列表
 */
function messageTimeFilter(
    messages: Message[],
    startDateDiff: number = 0,
    endDateDiff: number = 0,
) {
    const startTime = dayjs().startOf('day').subtract(startDateDiff, 'day');
    const endTime = dayjs().endOf('day').subtract(endDateDiff, 'day');

    return messages.filter((message) => {
        const createTime = dayjs(parseInt(message.createTime ?? ''));
        return createTime.isAfter(startTime) && createTime.isBefore(endTime);
    });
}

/**
 * 对消息按日期进行分组
 * @param {Message[]} messages 消息列表
 * @returns {Record<string, Message[]>} 按日期分组后的消息列表
 */
function messageGroupByDate(messages: Message[]) {
    return _.groupBy(
        messages,
        (m) =>
            dayjs(parseInt(m.createTime ?? ''))
                .format('YYYY-MM-DD'),
    );
}

/**
 * 对消息按小时进行分组
 * @param messages 消息列表
 * @returns 按小时分组后的消息列表
 */
function messageGroupByHour(messages: Message[]) {
    return _.groupBy(
        messages,
        (m) =>
            dayjs(parseInt(m.createTime ?? ''))
                .format('HH'),
    );
}

/**
 * 消息列表统计发言人数&消息数量
 * @param {Message[]} messages 消息列表
 * @returns {Object} 发言人数&消息数量
 * @returns {number} messageCount 消息数量
 * @returns {number} messagePersonCount 发言人数
 */
function messageStatistic(messages: Message[]) {
    const uniqueUserSet = new Set(messages.map((m) => m.sender));

    return {
        messageCount: messages.length,
        messagePersonCount: uniqueUserSet.size,
    };
}

/**
 * 消息列表按用户聚合发言条数
 * @param {Message[]} messages 消息列表
 * @returns {Record<string, number>} 按用户聚合的发言条数
 */
function messageByPersonCount(messages: Message[]) {
    const messageByPersonMap: Record<string, number> = {}; // 按人统计的消息数量

    for (const message of messages) {
        const userId = message.sender;
        messageByPersonMap[userId] = (messageByPersonMap[userId] || 0) + 1;
    }

    return messageByPersonMap;
}
