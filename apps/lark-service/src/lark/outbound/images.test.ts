import { describe, expect, it } from 'bun:test';

import {
    resolveImageReferences,
    type LarkImageRegistry,
    type LarkImageUploader,
} from './images';

interface Harness {
    deps: { registry: LarkImageRegistry; uploader: LarkImageUploader };
    lookups: string[];
    downloads: string[];
    uploads: number;
    peakConcurrentDownloads: number;
}

function harness(
    over: {
        registry?: Record<string, string> | null;
        download?: (url: string) => Promise<Buffer>;
        upload?: () => Promise<string | null>;
    } = {},
): Harness {
    const h = {
        lookups: [] as string[],
        downloads: [] as string[],
        uploads: 0,
        peakConcurrentDownloads: 0,
    };
    let inFlight = 0;

    const registry: LarkImageRegistry = {
        async lookup(registryId) {
            h.lookups.push(registryId);
            return over.registry === undefined
                ? { '1.png': 'https://tos.example/1.png' }
                : over.registry;
        },
        async download(url) {
            h.downloads.push(url);
            inFlight += 1;
            h.peakConcurrentDownloads = Math.max(h.peakConcurrentDownloads, inFlight);
            try {
                // 让出一次事件循环，否则每个 download 都同步跑完，并发上限测不出来。
                await new Promise((resolve) => setTimeout(resolve, 0));
                return over.download ? await over.download(url) : Buffer.from('bytes');
            } finally {
                inFlight -= 1;
            }
        },
    };

    const uploader: LarkImageUploader = {
        async uploadImage() {
            h.uploads += 1;
            return over.upload ? await over.upload() : 'img_v3_uploaded';
        },
    };

    // 返回 h 本身而不是它的副本：uploads / peakConcurrentDownloads 是数字，展开一次
    // 就冻在当时的值上，断言永远读到 0。
    return Object.assign(h, { deps: { registry, uploader } });
}

describe('resolveImageReferences 顺利的时候', () => {
    it('把 @N.png 引用换成飞书 image_key，alt 保留', async () => {
        const h = harness();
        const out = await resolveImageReferences('看图 ![我的图](1.png)', 'g1', h.deps);

        expect(out).toBe('看图 ![我的图](img_v3_uploaded)');
        expect(h.downloads).toEqual(['https://tos.example/1.png']);
    });

    it('带 @ 前缀的写法也认', async () => {
        const h = harness();
        expect(await resolveImageReferences('![x](@1.png)', 'g1', h.deps)).toBe(
            '![x](img_v3_uploaded)',
        );
    });

    it('注册表按传进来的全局 id 查，不是飞书那边的裸 id', async () => {
        // 图片是上游产出的，它按全局消息 id 注册。用飞书 id 查必定 miss，症状是每张图
        // 都"不可用"而且没有任何报错。
        const h = harness();
        await resolveImageReferences('![x](1.png)', 'global_msg_42', h.deps);
        expect(h.lookups).toEqual(['global_msg_42']);
    });

    it('alt 里带 $& 这类正则替换记号也照原样留着', async () => {
        const h = harness();
        expect(await resolveImageReferences('![a$&b](1.png)', 'g1', h.deps)).toBe(
            '![a$&b](img_v3_uploaded)',
        );
    });
});

describe('resolveImageReferences 不需要干活的时候', () => {
    it('没有图片引用时原样返回，连注册表都不查', async () => {
        const h = harness();
        expect(await resolveImageReferences('就是一句话', 'g1', h.deps)).toBe('就是一句话');
        expect(h.lookups).toEqual([]);
    });

    it('有引用但没给注册表 id 时原样返回，不查', async () => {
        // 占位符会一路留到 markdownToPostContent，被它当"未解析引用"跳过。
        const h = harness();
        expect(await resolveImageReferences('![x](1.png)', undefined, h.deps)).toBe('![x](1.png)');
        expect(h.lookups).toEqual([]);
    });
});

describe('resolveImageReferences 出问题的时候：一律降级成文字，绝不抛', () => {
    it('整张注册表查不到：占位符原样留着', async () => {
        const h = harness({ registry: null });
        expect(await resolveImageReferences('![x](1.png)', 'g1', h.deps)).toBe('![x](1.png)');
        expect(h.downloads).toEqual([]);
    });

    it('注册表是空的：同上', async () => {
        const h = harness({ registry: {} });
        expect(await resolveImageReferences('![x](1.png)', 'g1', h.deps)).toBe('![x](1.png)');
    });

    it('注册表里没有这张图：换成"不可用"', async () => {
        const h = harness({ registry: { '2.png': 'https://tos.example/2.png' } });
        expect(await resolveImageReferences('看 ![x](1.png)', 'g1', h.deps)).toBe(
            '看 (图片 1.png 不可用)',
        );
        expect(h.downloads).toEqual([]);
    });

    it('下载炸了：换成"处理失败"', async () => {
        const h = harness({
            download: async () => {
                throw new Error('HTTP 502');
            },
        });
        expect(await resolveImageReferences('看 ![x](1.png)', 'g1', h.deps)).toBe(
            '看 (图片 1.png 处理失败)',
        );
    });

    it('上传成功但平台没给 key：换成"上传失败"', async () => {
        const h = harness({ upload: async () => null });
        expect(await resolveImageReferences('看 ![x](1.png)', 'g1', h.deps)).toBe(
            '看 (图片 1.png 上传失败)',
        );
    });

    it('上传炸了：换成"处理失败"', async () => {
        const h = harness({
            upload: async () => {
                throw new Error('upload exploded');
            },
        });
        expect(await resolveImageReferences('看 ![x](1.png)', 'g1', h.deps)).toBe(
            '看 (图片 1.png 处理失败)',
        );
    });

    it('一张挂了不连坐另一张', async () => {
        const h = harness({
            registry: { '1.png': 'https://tos.example/1.png' },
            // 2.png 不在注册表里
        });
        expect(await resolveImageReferences('![a](1.png) 和 ![b](2.png)', 'g1', h.deps)).toBe(
            '![a](img_v3_uploaded) 和 (图片 2.png 不可用)',
        );
    });
});

describe('resolveImageReferences 的并发', () => {
    it('多张图并发处理，但同时在飞的下载不超过 5 个', async () => {
        // 一条消息里塞十几张图时，无上限并发会同时打满出网带宽，还容易踩飞书上传限流。
        const registry = Object.fromEntries(
            Array.from({ length: 7 }, (_, i) => [`${i + 1}.png`, `https://tos.example/${i + 1}.png`]),
        );
        const h = harness({ registry });
        const markdown = Array.from({ length: 7 }, (_, i) => `![p](${i + 1}.png)`).join(' ');

        const out = await resolveImageReferences(markdown, 'g1', h.deps);

        expect(h.downloads).toHaveLength(7);
        expect(h.uploads).toBe(7);
        expect(h.peakConcurrentDownloads).toBeGreaterThan(1);
        expect(h.peakConcurrentDownloads).toBeLessThanOrEqual(5);
        expect(out).not.toContain('.png)');
    });
});
