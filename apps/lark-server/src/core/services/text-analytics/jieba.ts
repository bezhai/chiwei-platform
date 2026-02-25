import { cloudSkipWords } from './word-utils';
import _ from 'lodash';
import http from '@http/client';
import { requestWithRetry } from '@inner/shared';

/**
 * 检查一个词是否有意义
 * @param word 输入的单词
 * @returns 是否是有意义的单词
 */
function isMeaningful(word: string): boolean {
    for (const char of word) {
        if (/\p{P}/u.test(char) || /\s/.test(char)) {
            return false;
        }
    }
    return true;
}

/**
 * 调用分词服务，批量提取关键词和权重
 * @param texts 文本数组
 * @param topN 每个文本提取的关键词数量
 * @returns 每个文本的关键词数组
 */
async function extractBatchWithWeight(
    texts: string[],
    topN: number,
): Promise<{ text: string; keywords: { word: string; weight: number }[] }[]> {
    try {
        const response = await requestWithRetry(
            () => http.post(
                `http://${process.env.AI_SERVER_HOST}:${process.env.AI_SERVER_PORT}/extract_batch`,
                {
                    texts,
                    top_n: topN,
                },
            ),
            { maxRetries: 3, retryDelay: 1000 }
        );
        return response.data;
    } catch (error) {
        console.error('Error calling segmentation service:', error);
        return []; // 返回空数组以防止服务调用失败
    }
}

/**
 * 分割数组为指定大小的批次
 * @param array 要分割的数组
 * @param chunkSize 每个批次的大小
 * @returns 分割后的数组
 */
function chunkArray<T>(array: T[], chunkSize: number): T[][] {
    const result: T[][] = [];
    for (let i = 0; i < array.length; i += chunkSize) {
        result.push(array.slice(i, i + chunkSize));
    }
    return result;
}

/**
 * 构建一周的词云
 * @param texts 文本数组
 * @param batchSize 每次处理的文本数量
 * @returns 词云结果，Map 对象，其中键是单词，值是累计的权重
 */
export async function buildWeeklyWordCloud(
    texts: string[],
    batchSize: number = 50, // 默认批次大小为 50
) {
    const result = new Map<string, number>();
    const wordMap = new Set<string>(cloudSkipWords);

    // 将文本分割为批次
    const textBatches = chunkArray(texts, batchSize);

    for (const batch of textBatches) {
        // 调用分词服务处理当前批次
        const batchRes = await extractBatchWithWeight(batch, 6);

        // 处理分词结果
        for (const text of batchRes) {
            const filteredWords: { word: string; weight: number }[] = [];
            let totalWeight = 0;

            for (const word of text.keywords) {
                if (wordMap.has(word.word)) {
                    continue;
                }
                if (isMeaningful(word.word)) {
                    filteredWords.push(word);
                    totalWeight += word.weight;
                }
            }

            for (const word of filteredWords) {
                _.update(result, word.word, (oldValue = 0) => oldValue + word.weight / totalWeight);
            }
        }
    }

    return result;
}
