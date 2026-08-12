// 一周的群发言 → 一张「词 → 分数」的表，给「水群」那张卡片上的词云用。
//
//     群发言 ──按 50 条一批──▶ tool-service /extract_batch ──▶ 每条消息的 top 6 关键词
//                                                                  │
//                    剔停用词 / 剔带标点的词 ──▶ 条内归一化 ──▶ 累加
//
// ## 归一化是**条内**的，这是整段唯一容易搬错的地方
//
// 每条消息的每个词记的是「它在这条消息里占的比重」（自己的权重 ÷ 这条消息里通过筛选的
// 词的权重和），然后跨消息累加。所以一条消息最多贡献 1 分，说得多的人靠条数上榜而不是
// 靠一条长消息。少了这一步，词云照样画得出来、只是排序不对 —— 没有任何人会发现。
//
// 停用词和带标点的词在**归一化之前**剔除，也就是不进分母。
//
// ## 分词服务挂了只丢这一批
//
// 与拆分前一致：`extractBatchWithWeight` 自己 catch 之后返回空数组，于是这一批消息什么
// 也不贡献，整张卡片照发。水群报告是个周报，缺几条比整张发不出来好。
//
// ## 那份停用词表是数据不是代码
//
// `stopwords.json` 与 channel-server 那份逐字相同（2141 条）。它没有进共享包：共享包的
// 判据是"这段代码需不需要知道任何具体渠道"，而这份表本身是渠道无关的 —— 但把它挪进
// packages 是一次新设计，不属于这次等价迁移。Task F 删掉 channel-server 那份之后这里
// 就是唯一一份。

import _ from 'lodash';
import { requestWithRetry } from '@inner/shared';

import stopwords from './stopwords.json';

/** 一条消息的分词结果。字段名是 tool-service 的口径。 */
export interface LarkExtractedKeywords {
    text: string;
    keywords: { word: string; weight: number }[];
}

/** 把一批文本交给分词服务。开发机打不到 tool-service，所以它是端口。 */
export type LarkKeywordExtractor = (
    texts: string[],
    topN: number,
) => Promise<LarkExtractedKeywords[]>;

/** 一次问多少条。上游默认 50，照搬 —— 一次问太多 tool-service 会超时。 */
export const WORD_CLOUD_BATCH_SIZE = 50;

/** 每条消息最多取几个关键词。上游写死 6，照搬。 */
export const WORD_CLOUD_TOP_N = 6;

/** 不进词云的词。跟 channel-server 那份逐字相同。 */
const SKIP = new Set<string>(stopwords);

/**
 * 一个词有没有意义：**整个词里不能有任何标点或空白**。
 *
 * 逐字照搬上游。顺带一个后果值得记下来：上游用 lodash 的 `_.update` 累加，而它把 `.`
 * 和 `[` 当成深路径分隔符 —— 只是这条筛选先把带标点的词全挡掉了，那条路走不到。这里
 * 用普通的 Map 累加，那个陷阱连同它一起消失，可观测行为不变。
 */
function isMeaningful(word: string): boolean {
    for (const char of word) {
        if (/\p{P}/u.test(char) || /\s/.test(char)) return false;
    }
    return true;
}

/**
 * 聚合。返回 `词 → 分数`，分数越大越靠前。
 *
 * @param texts 已经去过表情标记、非空、且不含链接的群发言（筛选在调用方，见 history-card.ts）
 */
export async function buildWeeklyWordCloud(
    extract: LarkKeywordExtractor,
    texts: string[],
    batchSize: number = WORD_CLOUD_BATCH_SIZE,
): Promise<Map<string, number>> {
    const cloud = new Map<string, number>();

    // 逐批串行，与上游一致：并发打分词服务没有额度保护。
    for (const batch of _.chunk(texts, batchSize)) {
        for (const message of await extract(batch, WORD_CLOUD_TOP_N)) {
            const kept = message.keywords.filter(
                (keyword) => !SKIP.has(keyword.word) && isMeaningful(keyword.word),
            );
            const total = kept.reduce((sum, keyword) => sum + keyword.weight, 0);
            for (const keyword of kept) {
                cloud.set(keyword.word, (cloud.get(keyword.word) ?? 0) + keyword.weight / total);
            }
        }
    }

    return cloud;
}

/** 打 tool-service 的那一跳。真身是 LaneRouter 的 axios 客户端（它注 x-ctx-lane 与 trace）。 */
export type LarkKeywordPost = (path: string, body: unknown) => Promise<{ data: unknown }>;

/** 跨服务契约。写错了 tool-service 返回 404，而 404 在这里被吞成"这一批没有词"。 */
export const EXTRACT_BATCH_PATH = '/extract_batch';

/**
 * 重试参数照搬上游（`requestWithRetry` 的 maxRetries 3 / retryDelay 1000，指数退避）。
 *
 * 只对连接错误和 5xx 重试 —— 那是 `requestWithRetry` 的默认判据，4xx 重试没有意义。
 * delay 之所以可覆盖，只是为了让"重试确实发生了"这件事在单元测试里可观测：真实的
 * 1s→2s→4s 会让一条用例跑七秒。
 */
export const KEYWORD_RETRY = { maxRetries: 3, retryDelay: 1000 } as const;

export function toolServiceKeywords(
    post: LarkKeywordPost,
    retry: { maxRetries: number; retryDelay: number } = KEYWORD_RETRY,
): LarkKeywordExtractor {
    return async (texts, topN) => {
        try {
            const response = await requestWithRetry(
                () => post(EXTRACT_BATCH_PATH, { texts, top_n: topN }),
                retry,
            );
            return response.data as LarkExtractedKeywords[];
        } catch (error) {
            // 上游同样是"记一条日志、这一批当空的"。整张周报不该因为分词挂了发不出来。
            console.error('[lark-word-cloud] tool-service could not segment this batch:', error);
            return [];
        }
    };
}
