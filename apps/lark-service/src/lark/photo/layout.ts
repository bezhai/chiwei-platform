// 两栏图墙怎么分。
//
// 飞书的卡片没有瀑布流：一行两栏，每栏纵向堆图，栏宽由一个整数权重比定。要让两栏看
// 起来一样高，得把图按**高宽比之和**尽量对半分，同时挑一个跟这个分法配得上的宽度比。
//
// 做法是穷举：五种飞书认的宽度比 × 所有把图分成左右两堆的方法，取"左栏实际高度 /
// 理想高度"最接近 1 的那一种。图最多 6 张，2^6 种分法乘 5 种比例，量很小。
//
// 拆分前这是 calcBestChunks，逐字等价重写，包括下面两处它自带的怪癖。

import _ from 'lodash';
import type { ImageForLark } from '@inner/pixiv-client';

/** 飞书卡片的栏宽只认整数权重，这五种是拆分前就在用的候选。 */
export type ColumnWeight = 1 | 2 | 3 | 4 | 5;

const WEIGHTS: readonly [ColumnWeight, ColumnWeight][] = [
    [1, 1],
    [5, 4],
    [4, 3],
    [3, 4],
    [4, 5],
];

export interface PhotoColumns {
    /** [左栏, 右栏]。 */
    columns: [ImageForLark[], ImageForLark[]];
    /** [左栏权重, 右栏权重]。 */
    weights: [ColumnWeight, ColumnWeight];
}

/** 从 arr 里取 n 个的所有组合。 */
function combinations<T>(arr: T[], n: number): T[][] {
    if (n === 0) return [[]];
    if (arr.length === 0) return [];

    const [first, ...rest] = arr;
    return [
        ...combinations(rest, n - 1).map((combination) => [first!, ...combination]),
        ...combinations(rest, n),
    ];
}

/**
 * 按高宽比把图分成左右两栏，顺带给出栏宽比。
 *
 * **两处怪癖，与拆分前逐字一致，本批不改**：
 *
 *   1. 候选分法取自「非空真子集」，所以**只有一张图时一个候选都没有**，两栏都是空的
 *      —— 「发图」只搜到一张时用户看到一张没有图的卡片。定时任务那条路正因如此才
 *      自己拼卡片、不走这里。
 *   2. 右栏用 `differenceBy(..., 'image_key')` 求补集，认的是 image_key 而不是位置。
 *      同一个 image_key 出现两次时右栏会把两份都排除掉。
 */
export function splitIntoColumns(photos: ImageForLark[]): PhotoColumns {
    const rates = photos.map((photo) => photo.height! / photo.width!);
    const sumRate = _.sum(rates);

    let bestRatio = 0;
    let bestWeights: [ColumnWeight, ColumnWeight] = [1, 1];
    let bestLeft: ImageForLark[] = [];
    let bestRight: ImageForLark[] = [];

    for (const weights of WEIGHTS) {
        // 左栏越窄，同样的图排下来越高。理想的左栏高度按权重反比分总高度。
        const idealLeft = (sumRate * weights[1]) / (weights[0] + weights[1]);

        const candidates = _.flatMap(_.range(1, photos.length), (size) =>
            combinations(_.range(photos.length), size),
        );

        for (const indexes of candidates) {
            const left = indexes.map((index) => photos[index]!);
            if (left.length <= 0) continue;
            const right = _.differenceBy(photos, left, 'image_key');
            const leftRate = _(indexes)
                .map((index) => rates[index]!)
                .sum();

            let ratio = leftRate / idealLeft;
            // 只从"左栏偏高"的那一侧往回归一化。偏矮的那一半直接跳过 —— 这是拆分前
            // 的写法，照搬。
            if (ratio >= 1) ratio = 1 / ratio;
            else continue;

            if (ratio > bestRatio) {
                bestRatio = ratio;
                bestWeights = weights;
                bestLeft = left;
                bestRight = right;
            }
        }
    }

    return { columns: [bestLeft, bestRight], weights: bestWeights };
}
