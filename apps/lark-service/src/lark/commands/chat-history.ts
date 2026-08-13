// 取一个飞书群过去 N 天的历史消息，给「水群」那张周报用。
//
//     [13 天前 0 点, 今天 23:59:59] ──切成 10 片──▶ 每片各自翻页 ──▶ 汇总
//                                        （并发）        （限速器串起来）
//
// ## 为什么要切片：这个接口按时间窗查，不按条数
//
// 一个两周的窗口在活跃群里有上万条，单线程翻页要几分钟。切 10 片并发翻，总请求数不变
// 但墙上时间除以 10。**切片数是 10，照搬** —— 它同时是打飞书的并发度，改它就是改额度
// 消耗的形状。
//
// ## 两个限速器是共享的，不是每片一个
//
// 飞书给这个接口的额度是 **40/s + 800/min**，按应用算而不是按调用点算。所以两个限速器
// 在装配期建一次、十片共用。少接一个的症状不是"慢一点"，是**偶尔整个失败** —— 超额时
// 飞书返回的是错误码，不是等待。
//
// 每片翻页之前先过这两道闸，顺序照搬（先分钟后秒）。等待预算也照搬：分钟闸最多等 60s、
// 秒闸最多等 10s，等不到就直接放行（`waitForAllowance` 返回 false 时上游不看返回值）。
//
// ## 一片失败就整个抛
//
// 与拆分前一致（`Promise.all`）。一份缺了十分之一的周报没有任何提示，读的人会当它是真的。
//
// ## 历史消息与入站事件不是同一个形状
//
// 历史接口回来的 `sender.id` 对真人是 open_id、对 bot 是 **app_id**，而 `id_type` 是唯一
// 能区分二者的东西 —— 认不出来的话龙王榜上会出现赤尾自己。正文也简单得多：只认 `text`
// 字段，富文本 / 图片一律读成空串（拆分前就是这样）。所以这里有一个自己的小领域对象，
// 不复用入站那条链的 LarkInboundMessage。

import dayjs from 'dayjs';
import { RateLimiter, TextUtils } from '@inner/shared';

import type { LarkMessageInfo, LarkMessageQuery, LarkOutboundApi } from '../outbound/lark-api';

/** 一条历史消息，只留「水群」真的用得上的四件事。 */
export interface LarkHistoryMessage {
    messageId: string;
    /** 飞书给的 sender.id：真人是 open_id、bot 是 app_id。龙王榜的 `<at id=…>` 用它。 */
    senderId: string;
    /** `id_type === 'app_id'` 就是机器人。 */
    isRobot: boolean;
    /** 毫秒时间戳字符串。飞书没给就是 undefined。 */
    createTime?: string;
    /** 去掉 @ 占位符与 `[表情]` / `<标签>` 之后的正文。词云吃它。 */
    text: string;
}

/**
 * 取历史。
 *
 * @param startDayOffset 从**几天前的零点**开始
 * @param endDayOffset   到**几天前的当天最后一刻**为止（0 就是今天）
 */
export type LarkChatHistory = (
    chatId: string,
    startDayOffset: number,
    endDayOffset: number,
) => Promise<LarkHistoryMessage[]>;

/** 一个窗口切几片。**同时是打飞书的并发度**，拆分前就是 10。 */
export const HISTORY_SPLIT_SIZE = 10;

/** 飞书给这个接口的额度。按应用算，所以两个限速器全进程共享。 */
export const HISTORY_RATE_PER_MINUTE = 800;
export const HISTORY_RATE_PER_SECOND = 40;

/** 两道闸各自最多愿意等多久。照搬上游。 */
const MINUTE_BUDGET_MS = 60 * 1000;
const SECOND_BUDGET_MS = 10 * 1000;

/**
 * 把 `[start, end]` 切成 splitSize 段，**首尾相接不重叠**，最后一段一直覆盖到 end。
 *
 * 逐字照搬上游：步长向下取整，前 n-1 段是 `[s+i*step, s+(i+1)*step-1]`，最后一段吃掉
 * 除不尽的余数。重叠会让同一条消息被数两遍，缺口会让它一条都数不到。
 */
export function splitTime(start: number, end: number, splitSize: number): number[][] {
    if (splitSize <= 0 || start >= end) {
        throw new Error('Invalid input: splitSize must be > 0 and start must be < end');
    }

    const step = Math.floor((end - start) / splitSize);
    const result: number[][] = [];
    for (let i = 0; i < splitSize - 1; i++) {
        result.push([start + i * step, start + (i + 1) * step - 1]);
    }
    result.push([start + (splitSize - 1) * step, end]);
    return result;
}

/**
 * 限速器看时间的方式。
 *
 * `@inner/shared` 只导出了 RateLimiter 本身、没导出它那个时钟接口，所以这里按**结构**
 * 对上（TS 认结构不认名字）。为了一个测试口子去改共享包的导出面不划算 —— 那个包被三个
 * 服务共用，加导出是给所有人加维护面。
 */
export interface LarkHistoryClock {
    now(): number;
    sleep(ms: number): Promise<void>;
}

export interface LarkChatHistoryDeps {
    api: Pick<LarkOutboundApi, 'listMessages'>;
    /** "现在"。注入它是因为窗口按自然日算，而自然日边界只有可控的时钟才测得出来。 */
    now: () => Date;
    /** 限速器的时间源。注入它只为了让"限速器确实在路径上"可观测，生产用真实时钟。 */
    clock?: LarkHistoryClock;
}

export function larkChatHistory(deps: LarkChatHistoryDeps): LarkChatHistory {
    // **装配期建一次，十片共用**：额度是按应用算的，每片一个等于把额度乘以十。
    const perMinute = new RateLimiter(HISTORY_RATE_PER_MINUTE, 60 * 1000, deps.clock);
    const perSecond = new RateLimiter(HISTORY_RATE_PER_SECOND, 1000, deps.clock);

    /** 一片：从 startTime 到 endTime，翻到 has_more 为假为止。 */
    async function slice(chatId: string, startTime: number, endTime: number) {
        const collected: LarkHistoryMessage[] = [];
        let pageToken: string | undefined;

        for (;;) {
            await perMinute.waitForAllowance(MINUTE_BUDGET_MS);
            await perSecond.waitForAllowance(SECOND_BUDGET_MS);

            const query: LarkMessageQuery = { chatId, startTime, endTime, pageToken };
            const page = await deps.api.listMessages(query);
            collected.push(...page.items.filter(worthCounting).map(historyMessage));
            pageToken = page.pageToken;

            if (!page.hasMore) return collected;
        }
    }

    return async (chatId, startDayOffset, endDayOffset) => {
        const at = dayjs(deps.now());
        const startTime = at.startOf('day').subtract(startDayOffset, 'day').unix();
        const endTime = at.endOf('day').subtract(endDayOffset, 'day').unix();

        // 十片同时出发。一片抛整个抛 —— 缺了十分之一的周报不会有任何提示。
        const slices = await Promise.all(
            splitTime(startTime, endTime, HISTORY_SPLIT_SIZE).map(([start, end]) =>
                slice(chatId, start!, end!),
            ),
        );
        return slices.flat();
    };
}

/**
 * 撤回过的消息**仍然出现在历史里**，靠 deleted 区分；合并转发是一整包别人的消息，
 * 拿它统计等于把别的群的发言算进本群。两条与拆分前逐字相同。
 */
function worthCounting(message: LarkMessageInfo): boolean {
    return !message.deleted && message.messageType !== 'merge_forward';
}

function historyMessage(message: LarkMessageInfo): LarkHistoryMessage {
    return {
        messageId: message.messageId,
        // 飞书没给发送者时记 'unknown' 而不是空串：调用方按"非空"过滤，两者不等价。
        senderId: message.senderId ?? 'unknown',
        isRobot: message.senderIdType === 'app_id',
        createTime: message.createTime,
        text: historyText(message.content),
    };
}

/**
 * 历史消息的正文。**只认 `text` 字段**：富文本、图片、表情包一律读成空串，与拆分前
 * 一致（那边的 buildContentFromHistory 也只看 content.text）。
 *
 * 解析失败不抛：一条读不懂的历史消息不该让整份周报发不出来。
 */
function historyText(content: string | undefined): string {
    if (!content) return '';
    try {
        const payload = JSON.parse(content) as { text?: string };
        return TextUtils.removeEmoji(TextUtils.clearText(payload.text ?? ''));
    } catch (error) {
        console.error('[lark-history] cannot read a history message body:', error);
        return '';
    }
}
