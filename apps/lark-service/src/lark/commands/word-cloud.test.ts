// 词云：把一周的群发言交给 tool-service 分词，按权重聚合成「词 → 分数」。
//
// 这里要钉的是**聚合口径**，因为它错了词云照样画得出来、只是排序不对，没有任何人会发现：
//
//   * 每条消息的权重先在自己内部归一化（除以这条消息里通过筛选的词的权重和），再累加。
//     少了归一化，一条长消息就能把整张词云压平。
//   * 停用词在**归一化之前**剔除 —— 它们不进分母。
//   * 带标点或空白的词整个丢掉，同样不进分母。
//   * 分词服务挂了只丢这一批，不抛 —— 上游是"整批返回空数组"，照搬。

import { describe, expect, it } from 'bun:test';

import {
    KEYWORD_RETRY,
    WORD_CLOUD_BATCH_SIZE,
    WORD_CLOUD_TOP_N,
    buildWeeklyWordCloud,
    toolServiceKeywords,
    type LarkExtractedKeywords,
} from './word-cloud';

/** 一个可辨识的停用词。它必须真的在那份表里，否则下面那条用例就是空跑。 */
const A_STOPWORD = '一样';

function extractor(
    answers: Record<string, { word: string; weight: number }[]>,
): {
    extract: (texts: string[], topN: number) => Promise<LarkExtractedKeywords[]>;
    asked: { texts: string[]; topN: number }[];
} {
    const asked: { texts: string[]; topN: number }[] = [];
    return {
        asked,
        extract: async (texts, topN) => {
            asked.push({ texts, topN });
            return texts.map((text) => ({ text, keywords: answers[text] ?? [] }));
        },
    };
}

describe('词云的聚合口径', () => {
    it('每条消息内部先归一化再累加', async () => {
        const { extract } = extractor({
            甲: [
                { word: '刻晴', weight: 3 },
                { word: '原神', weight: 1 },
            ],
            乙: [{ word: '原神', weight: 9 }],
        });

        const cloud = await buildWeeklyWordCloud(extract, ['甲', '乙']);

        // 甲：3/4 与 1/4；乙：9/9 = 1。所以原神 = 0.25 + 1 = 1.25。
        expect(cloud.get('刻晴')).toBeCloseTo(0.75, 10);
        expect(cloud.get('原神')).toBeCloseTo(1.25, 10);
    });

    it('停用词剔除，而且不进分母', async () => {
        const { extract } = extractor({
            甲: [
                { word: A_STOPWORD, weight: 3 },
                { word: '刻晴', weight: 1 },
            ],
        });

        const cloud = await buildWeeklyWordCloud(extract, ['甲']);

        expect(cloud.has(A_STOPWORD)).toBe(false);
        // 分母只剩「刻晴」那 1，所以它拿满分而不是 1/4。
        expect(cloud.get('刻晴')).toBeCloseTo(1, 10);
    });

    it('带标点或空白的词整个丢掉，也不进分母', async () => {
        const { extract } = extractor({
            甲: [
                { word: '真的?', weight: 3 },
                { word: '刻 晴', weight: 5 },
                { word: '原神', weight: 1 },
            ],
        });

        const cloud = await buildWeeklyWordCloud(extract, ['甲']);

        expect([...cloud.keys()]).toEqual(['原神']);
        expect(cloud.get('原神')).toBeCloseTo(1, 10);
    });

    it('一条消息一个词都没通过筛选时什么也不加（不产生 NaN）', async () => {
        const { extract } = extractor({ 甲: [{ word: '呃…', weight: 3 }] });

        expect([...(await buildWeeklyWordCloud(extract, ['甲'])).entries()]).toEqual([]);
    });

    // 批次大小和 top_n 是跨服务契约的一部分：一次问太多条 tool-service 会超时，而
    // top_n 决定每条消息最多贡献几个词。
    it('按批切、每批问同一个 top_n', async () => {
        const { extract, asked } = extractor({});
        const texts = Array.from({ length: WORD_CLOUD_BATCH_SIZE + 3 }, (_, i) => `t${i}`);

        await buildWeeklyWordCloud(extract, texts);

        expect(asked.map((call) => call.texts.length)).toEqual([WORD_CLOUD_BATCH_SIZE, 3]);
        expect(asked.every((call) => call.topN === WORD_CLOUD_TOP_N)).toBe(true);
    });
});

describe('分词服务的适配器', () => {
    it('打 /extract_batch，把 texts 和 top_n 递过去', async () => {
        const calls: { path: string; body: unknown }[] = [];
        const extract = toolServiceKeywords(async (path, body) => {
            calls.push({ path, body });
            return { data: [{ text: '甲', keywords: [{ word: '刻晴', weight: 1 }] }] };
        });

        expect(await extract(['甲'], 6)).toEqual([
            { text: '甲', keywords: [{ word: '刻晴', weight: 1 }] },
        ]);
        expect(calls).toEqual([{ path: '/extract_batch', body: { texts: ['甲'], top_n: 6 } }]);
    });

    // 分词挂了不该让整张水群报告黄掉 —— 上游就是"记一条日志、这一批当空的"，照搬。
    it('打不通只丢这一批，不抛', async () => {
        const extract = toolServiceKeywords(async () => {
            throw new Error('tool-service is down');
        });

        expect(await extract(['甲'], 6)).toEqual([]);
    });

    // 上游把这一跳包在 requestWithRetry 里（连接错误和 5xx 重试三次，指数退避）。少了它
    // 一次 tool-service 重启就让当天的词云整块空掉，而现象只是"词云比平时少"。
    it('连接错误重试三次之后才放弃', async () => {
        let attempts = 0;
        const extract = toolServiceKeywords(
            async () => {
                attempts += 1;
                throw Object.assign(new Error('connect ECONNRESET'), { code: 'ECONNRESET' });
            },
            { maxRetries: KEYWORD_RETRY.maxRetries, retryDelay: 0 },
        );

        expect(await extract(['甲'], 6)).toEqual([]);
        expect(attempts).toBe(KEYWORD_RETRY.maxRetries + 1);
    });

    it('重试之后成功就用那次的结果', async () => {
        let attempts = 0;
        const extract = toolServiceKeywords(
            async () => {
                attempts += 1;
                if (attempts === 1) {
                    throw Object.assign(new Error('boom'), { response: { status: 502 } });
                }
                return { data: [{ text: '甲', keywords: [] }] };
            },
            { maxRetries: 3, retryDelay: 0 },
        );

        expect(await extract(['甲'], 6)).toEqual([{ text: '甲', keywords: [] }]);
        expect(attempts).toBe(2);
    });

    // 4xx 不重试：重试一个 404 只是把同一个错误多打三遍。
    it('4xx 不重试', async () => {
        let attempts = 0;
        const extract = toolServiceKeywords(
            async () => {
                attempts += 1;
                throw Object.assign(new Error('nope'), { response: { status: 404 } });
            },
            { maxRetries: 3, retryDelay: 0 },
        );

        await extract(['甲'], 6);
        expect(attempts).toBe(1);
    });

    it('分词失败的那一批不影响别的批次', async () => {
        let call = 0;
        const extract = toolServiceKeywords(async () => {
            call += 1;
            if (call === 1) throw new Error('tool-service hiccup');
            return { data: [{ text: '乙', keywords: [{ word: '原神', weight: 1 }] }] };
        });
        const texts = Array.from({ length: WORD_CLOUD_BATCH_SIZE + 1 }, (_, i) => `t${i}`);

        const cloud = await buildWeeklyWordCloud(extract, texts);

        expect(cloud.get('原神')).toBeCloseTo(1, 10);
    });
});
