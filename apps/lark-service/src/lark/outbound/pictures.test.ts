import { describe, expect, it } from 'bun:test';

import { larkPictureRows, type LarkPictureDeps } from './pictures';

interface Harness {
    deps: LarkPictureDeps;
    signed: string[];
    downloaded: string[];
    uploaded: number;
    peakConcurrentDownloads: number;
}

function harness(
    over: {
        sign?: (fileName: string) => Promise<string | null>;
        download?: (url: string) => Promise<Buffer>;
        upload?: () => Promise<string | null>;
    } = {},
): Harness {
    const h = {
        signed: [] as string[],
        downloaded: [] as string[],
        uploaded: 0,
        peakConcurrentDownloads: 0,
    };
    let inFlight = 0;

    const deps: LarkPictureDeps = {
        async sign(fileName) {
            h.signed.push(fileName);
            return over.sign ? await over.sign(fileName) : `https://tos.example/${fileName}?sig=now`;
        },
        async download(url) {
            h.downloaded.push(url);
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
        uploader: {
            async uploadImage() {
                h.uploaded += 1;
                return over.upload ? await over.upload() : 'img_v3_uploaded';
            },
        },
    };

    // 返回 h 本身而不是它的副本：uploaded / peakConcurrentDownloads 是数字，展开一次
    // 就冻在当时的值上，断言永远读到 0。
    return Object.assign(h, { deps });
}

describe('larkPictureRows 顺利的时候', () => {
    it('句柄现签 → 下载 → 上传，产出一行 img 节点', async () => {
        const h = harness();
        const rows = await larkPictureRows(['pictures/cat.png'], h.deps);

        expect(rows).toEqual([[{ tag: 'img', image_key: 'img_v3_uploaded' }]]);
        expect(h.signed).toEqual(['pictures/cat.png']);
        expect(h.downloaded).toEqual(['https://tos.example/pictures/cat.png?sig=now']);
        expect(h.uploaded).toBe(1);
    });

    it('签的是队列里那个永久句柄本身，不是别的什么', async () => {
        // 队列里传的是对象存储的 file_name。拿别的东西（比如消息 id）去签，症状是
        // 每张图都签不出来，而且全程无报错。
        const h = harness();
        await larkPictureRows(['2026/09/a-b-c.jpg'], h.deps);
        expect(h.signed).toEqual(['2026/09/a-b-c.jpg']);
    });

    it('多张图按给的顺序产出行', async () => {
        const h = harness({ upload: async () => `img_v3_${Math.random()}` });
        const rows = await larkPictureRows(['a.png', 'b.png', 'c.png'], h.deps);

        expect(rows).toHaveLength(3);
        expect(h.signed).toEqual(['a.png', 'b.png', 'c.png']);
    });

    it('没有图时一行都不产出，一次都不签', async () => {
        const h = harness();
        expect(await larkPictureRows([], h.deps)).toEqual([]);
        expect(h.signed).toEqual([]);
    });
});

describe('larkPictureRows 出问题的时候：降级成一行文字，绝不抛', () => {
    it('现签拿不到地址：换成"取不到地址"，不下载也不上传', async () => {
        const h = harness({ sign: async () => null });
        expect(await larkPictureRows(['cat.png'], h.deps)).toEqual([
            [{ tag: 'md', text: '(第 1 张图取不到地址)' }],
        ]);
        expect(h.downloaded).toEqual([]);
        expect(h.uploaded).toBe(0);
    });

    it('现签那一跳炸了：换成"处理失败"', async () => {
        const h = harness({
            sign: async () => {
                throw new Error('tool-service 500');
            },
        });
        expect(await larkPictureRows(['cat.png'], h.deps)).toEqual([
            [{ tag: 'md', text: '(第 1 张图处理失败)' }],
        ]);
        expect(h.uploaded).toBe(0);
    });

    it('下载炸了：换成"处理失败"', async () => {
        const h = harness({
            download: async () => {
                throw new Error('HTTP 502');
            },
        });
        expect(await larkPictureRows(['cat.png'], h.deps)).toEqual([
            [{ tag: 'md', text: '(第 1 张图处理失败)' }],
        ]);
        expect(h.uploaded).toBe(0);
    });

    it('上传成功但飞书没给 key：换成"上传失败"', async () => {
        const h = harness({ upload: async () => null });
        expect(await larkPictureRows(['cat.png'], h.deps)).toEqual([
            [{ tag: 'md', text: '(第 1 张图上传失败)' }],
        ]);
    });

    it('上传炸了：换成"处理失败"', async () => {
        const h = harness({
            upload: async () => {
                throw new Error('upload exploded');
            },
        });
        expect(await larkPictureRows(['cat.png'], h.deps)).toEqual([
            [{ tag: 'md', text: '(第 1 张图处理失败)' }],
        ]);
    });

    it('一张挂了不连坐另一张，位置照旧', async () => {
        const h = harness({
            sign: async (fileName) => (fileName === 'bad.png' ? null : `https://tos/${fileName}`),
        });
        expect(await larkPictureRows(['bad.png', 'good.png'], h.deps)).toEqual([
            [{ tag: 'md', text: '(第 1 张图取不到地址)' }],
            [{ tag: 'img', image_key: 'img_v3_uploaded' }],
        ]);
    });

    it('全挂了也只是几行文字，不抛', async () => {
        const h = harness({ sign: async () => null });
        const rows = await larkPictureRows(['a.png', 'b.png'], h.deps);
        expect(rows).toEqual([
            [{ tag: 'md', text: '(第 1 张图取不到地址)' }],
            [{ tag: 'md', text: '(第 2 张图取不到地址)' }],
        ]);
    });
});

describe('larkPictureRows 的并发', () => {
    it('多张图并发处理，但同时在飞的下载不超过 5 个', async () => {
        // 一次带十几张图不罕见。无上限并发会同时打满出网带宽，也容易撞飞书的上传
        // 限流 —— 而撞了限流的后果是这一批图全部降级。
        const h = harness();
        const fileNames = Array.from({ length: 7 }, (_, i) => `${i + 1}.png`);

        const rows = await larkPictureRows(fileNames, h.deps);

        expect(h.downloaded).toHaveLength(7);
        expect(h.uploaded).toBe(7);
        expect(h.peakConcurrentDownloads).toBeGreaterThan(1);
        expect(h.peakConcurrentDownloads).toBeLessThanOrEqual(5);
        expect(rows.every((row) => row[0]!.tag === 'img')).toBe(true);
    });
});
