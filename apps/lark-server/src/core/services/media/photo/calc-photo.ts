import _ from 'lodash';
import { ImageForLark } from 'types/pixiv';

function combinations<T>(arr: T[], n: number): T[][] {
    if (n === 0) return [[]];
    if (arr.length === 0) return [];

    const [first, ...rest] = arr;
    const combsWithFirst = combinations(rest, n - 1).map((comb) => [first, ...comb]);
    const combsWithoutFirst = combinations(rest, n);

    return [...combsWithFirst, ...combsWithoutFirst];
}

type ValidWeight = 1 | 2 | 3 | 4 | 5;

/**
 * 计算最佳的图片分组方式
 * @param images 要处理的图片数组
 * @returns {
 *   chunks: 按最佳比例分成的两组图片数组 [左侧图片组, 右侧图片组]
 *   weights: 最佳的宽度比例 [左侧权重, 右侧权重] 例如[4,3]表示左右宽度比为4:3
 * }
 */
export function calcBestChunks(images: ImageForLark[]): {
    chunks: [ImageForLark[], ImageForLark[]];
    weights: [ValidWeight, ValidWeight];
} {
    // 计算每张图片的比率
    const rates = images.map((img) => img.height! / img.width!);
    const sumRate = _.sum(rates);

    const weights: [ValidWeight, ValidWeight][] = [
        [1, 1],
        [5, 4],
        [4, 3],
        [3, 4],
        [4, 5],
    ];

    let targetRate = 0;
    let targetWeight: [ValidWeight, ValidWeight] = [1, 1];
    let targetLeft: ImageForLark[] = [];
    let targetRight: ImageForLark[] = [];

    // 遍历所有可能的权重组合
    for (const weight of weights) {
        const leftRate = (sumRate * weight[1]) / (weight[0] + weight[1]);

        // 生成所有可能的分组
        const allCombinations = _.flatMap(_.range(1, images.length), (n) =>
            combinations(_.range(images.length), n),
        );

        for (const indexCombination of allCombinations) {
            const chooseImages = indexCombination.map((idx) => images[idx]);
            const chooseRightImages = _.differenceBy(images, chooseImages, 'image_key');

            const currLeftSumRate = _(indexCombination)
                .map((idx) => rates[idx])
                .sum();

            if (chooseImages.length <= 0) {
                continue;
            }

            let ratio = currLeftSumRate / leftRate;
            if (ratio >= 1) {
                ratio = 1.0 / ratio;
            } else {
                continue;
            }

            if (ratio > targetRate) {
                targetWeight = weight;
                targetLeft = chooseImages;
                targetRight = chooseRightImages;
                targetRate = ratio;
            }
        }
    }

    return {
        chunks: [targetLeft, targetRight],
        weights: targetWeight,
    };
}
