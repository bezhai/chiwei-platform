// 两栏图墙的分栏。纯计算，没有 I/O。

import { describe, expect, it } from 'bun:test';
import type { ImageForLark } from '@inner/pixiv-client';

import { splitIntoColumns } from './layout';

function photo(key: string, width: number, height: number): ImageForLark {
    return { pixiv_addr: `${key}.png`, image_key: key, width, height };
}

function keys(photos: ImageForLark[]): string[] {
    return photos.map((p) => p.image_key!);
}

describe('分栏', () => {
    // 两张一样高瘦的图，一边一张、等宽：这是最直觉的那个答案，也是"算法没接反"的锚点。
    it('两张等比例的图各占一栏，权重 1:1', () => {
        const { columns, weights } = splitIntoColumns([
            photo('a', 100, 100),
            photo('b', 100, 100),
        ]);

        expect(keys(columns[0])).toEqual(['a']);
        expect(keys(columns[1])).toEqual(['b']);
        expect(weights).toEqual([1, 1]);
    });

    // 一张长图配两张方图：长图独占一栏，两张方图叠在另一栏，两栏高度才差不多。
    it('把总高度切得最匀的那种分法胜出', () => {
        const { columns, weights } = splitIntoColumns([
            photo('a', 100, 100),
            photo('b', 100, 100),
            photo('c', 100, 200),
        ]);

        expect(keys(columns[0])).toEqual(['c']);
        expect(keys(columns[1])).toEqual(['a', 'b']);
        expect(weights).toEqual([1, 1]);
    });

    it('每张图都恰好落在一栏里，不重不漏', () => {
        const photos = [
            photo('a', 100, 320),
            photo('b', 100, 90),
            photo('c', 100, 140),
            photo('d', 100, 60),
        ];

        const { columns } = splitIntoColumns(photos);

        expect([...keys(columns[0]), ...keys(columns[1])].sort()).toEqual(['a', 'b', 'c', 'd']);
    });

    it('权重只用飞书认的那五种比例', () => {
        const allowed = [
            [1, 1],
            [5, 4],
            [4, 3],
            [3, 4],
            [4, 5],
        ];
        const { weights } = splitIntoColumns([
            photo('a', 100, 400),
            photo('b', 100, 100),
            photo('c', 100, 110),
        ]);

        expect(allowed).toContainEqual([...weights]);
    });

    // **这是当前行为，不是期望行为。** 分法从"非空真子集"里挑，只有一张图时一个候选
    // 都没有，于是两栏都是空的 —— 「发图」只搜到一张时用户看到的是一张没有图的卡片。
    // 与拆分前逐字一致（定时任务那条路正因如此才自己拼卡片，不走这里）。登记在案。
    it('只有一张图时两栏都是空的', () => {
        const { columns, weights } = splitIntoColumns([photo('a', 100, 100)]);

        expect(columns).toEqual([[], []]);
        expect(weights).toEqual([1, 1]);
    });

    it('一张都没有时也交得出一个形状', () => {
        expect(splitIntoColumns([])).toEqual({ columns: [[], []], weights: [1, 1] });
    });
});
