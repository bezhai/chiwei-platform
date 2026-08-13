// 「凑够 N 张飞书发得出去的图」这件事。
//
// 候选未必能用（对象存储里那个文件没了、飞书拒收），所以这是一个补页循环而不是一次
// 查询。三种失效各有各的静默：补页时不排除已试过的地址 → 反复抽到同一张失败的图；
// 没有"没有进展就停"的兜底 → 死循环打库；显式顺序丢了 → 详情卡片张冠李戴。

import { describe, expect, it } from 'bun:test';
import { StatusMode, type ImageForLark, type ListPixivImageDto } from '@inner/pixiv-client';

import type { LarkPhotoLibrary, PhotoPage, PhotoPageRequest } from './library';
import { readyPhotos, type ReadyPhotoDeps } from './ready';

function query(overrides: Partial<ListPixivImageDto> = {}): ListPixivImageDto {
    return {
        status: StatusMode.VISIBLE,
        page: 1,
        page_size: 2,
        random_mode: false,
        ...overrides,
    };
}

/** 带 key 的图已经在飞书上；不带的要先下载再传。 */
function photo(pixivAddr: string, imageKey?: string): ImageForLark {
    return {
        pixiv_addr: pixivAddr,
        image_key: imageKey,
        tos_file_name: imageKey ? undefined : `${pixivAddr}.png`,
    };
}

interface Rig {
    deps: ReadyPhotoDeps;
    asked: PhotoPageRequest[];
    read: string[];
    uploaded: Buffer[];
    noted: { pixiv_addr: string; image_key: string; width: number; height: number }[];
}

function rig(
    candidates: (request: PhotoPageRequest) => Promise<PhotoPage>,
    overrides: {
        bytes?: (tosFileName: string) => Promise<Buffer>;
        upload?: (bytes: Buffer) => Promise<string | null>;
        noteLarkImageKey?: () => Promise<void>;
    } = {},
): Rig {
    const asked: PhotoPageRequest[] = [];
    const read: string[] = [];
    const uploaded: Buffer[] = [];
    const noted: Rig['noted'] = [];

    const library: LarkPhotoLibrary = {
        candidates: (request) => {
            asked.push(request);
            return candidates(request);
        },
        bytes: async (tosFileName) => {
            read.push(tosFileName);
            return overrides.bytes
                ? await overrides.bytes(tosFileName)
                : Buffer.from('image-bytes');
        },
        noteLarkImageKey: async (upload) => {
            noted.push(upload);
            if (overrides.noteLarkImageKey) await overrides.noteLarkImageKey();
        },
    };

    return {
        asked,
        read,
        uploaded,
        noted,
        deps: {
            library,
            resize: async (bytes) => ({ bytes, width: 100, height: 200 }),
            upload: async (bytes) => {
                uploaded.push(bytes);
                return overrides.upload ? await overrides.upload(bytes) : 'uploaded-key';
            },
        },
    };
}

// ---------------------------------------------------------------------------

describe('已经有 image_key 的图', () => {
    it('原样交出去，一次都不下载、不上传、不回写', async () => {
        const it_ = rig(async () => ({ photos: [photo('a', 'a-key')], exhausted: true }));

        const photos = await readyPhotos(it_.deps)(query({ page_size: 1 }));

        expect(photos).toEqual([photo('a', 'a-key')]);
        expect(it_.read).toEqual([]);
        expect(it_.uploaded).toEqual([]);
        expect(it_.noted).toEqual([]);
    });
});

describe('还没传过的图', () => {
    it('下载 → 缩 → 传飞书 → 把 image_key 回写进库', async () => {
        const it_ = rig(async () => ({ photos: [photo('a')], exhausted: true }));

        const photos = await readyPhotos(it_.deps)(query({ page_size: 1 }));

        expect(photos).toEqual([
            { pixiv_addr: 'a', image_key: 'uploaded-key', tos_file_name: 'a.png', width: 100, height: 200 },
        ]);
        expect(it_.read).toEqual(['a.png']);
        expect(it_.uploaded.map((bytes) => bytes.toString())).toEqual(['image-bytes']);
        // 回写的宽高是**缩之后的**，不是库里原来那份 —— 卡片的分栏权重按它算。
        expect(it_.noted).toEqual([
            { pixiv_addr: 'a', image_key: 'uploaded-key', width: 100, height: 200 },
        ]);
    });

    it.each([
        ['库里没有对象名', { photos: [{ pixiv_addr: 'a' }] as ImageForLark[] }, {}],
        ['取回来是空字节', { photos: [photo('a')] }, { bytes: async () => Buffer.alloc(0) }],
        ['飞书没给 image_key', { photos: [photo('a')] }, { upload: async () => null }],
        [
            '中途抛了',
            { photos: [photo('a')] },
            {
                bytes: async () => {
                    throw new Error('minio is down');
                },
            },
        ],
    ])('%s：这一张不算数，整批照常交（不抛）', async (_name, page, overrides) => {
        const it_ = rig(async () => ({ ...page, exhausted: true }), overrides);

        const photos = await readyPhotos(it_.deps)(query({ page_size: 1 }));

        expect(photos).toEqual([]);
    });

    // **这是当前行为，不是期望行为。** 回写发生在图已经传上飞书之后，它失败时这一张
    // 明明能发却被丢掉了。与拆分前逐字一致（回写那一行就在同一个 try 里），登记在案。
    it('回写失败会把这一张也丢掉', async () => {
        const it_ = rig(async () => ({ photos: [photo('a')], exhausted: true }), {
            noteLarkImageKey: async () => {
                throw new Error('mongo is down');
            },
        });

        const photos = await readyPhotos(it_.deps)(query({ page_size: 1 }));

        expect(photos.map((p) => p.pixiv_addr)).toEqual([]);
    });
});

describe('补页', () => {
    // 第一张下载失败之后必须接着往下取，而且**不能再套一次页码偏移** —— 那会把
    // 第二页整段跳过去。
    it('一张失败就用游标接着取，页码只在首次生效', async () => {
        const it_ = rig(async (request) => {
            if (!request.cursor) {
                return {
                    photos: [photo('c'), photo('d', 'd-key')],
                    cursor: { mode: 'ordered', updateTime: new Date('2026-07-20'), id: 'd-id' },
                    exhausted: false,
                };
            }
            return {
                photos: [photo('e', 'e-key')],
                cursor: { mode: 'ordered', updateTime: new Date('2026-07-19'), id: 'e-id' },
                exhausted: true,
            };
        }, {
            bytes: async (tosFileName) => {
                if (tosFileName === 'c.png') throw new Error('missing object');
                return Buffer.from('image-bytes');
            },
        });

        const photos = await readyPhotos(it_.deps)(query({ page: 2, page_size: 2 }));

        expect(photos.map((p) => p.pixiv_addr)).toEqual(['d', 'e']);
        expect(it_.asked).toHaveLength(2);
        expect(it_.asked[0]!.cursor).toBeUndefined();
        expect(it_.asked[1]!.cursor).toEqual({
            mode: 'ordered',
            updateTime: new Date('2026-07-20'),
            id: 'd-id',
        });
        // 页码原样传下去：翻页由游标负责，真身自己知道续页不再 $skip。
        expect(it_.asked[1]!.query.page).toBe(2);
    });

    it('只要还差几张，下一页就只要差的那几张', async () => {
        const it_ = rig(async (request) => ({
            photos: request.cursor ? [photo('b', 'b-key')] : [photo('a', 'a-key')],
            cursor: { mode: 'ordered', updateTime: new Date('2026-07-20'), id: 'x' },
            exhausted: Boolean(request.cursor),
        }));

        await readyPhotos(it_.deps)(query({ page_size: 3 }));

        expect(it_.asked.map((request) => request.limit)).toEqual([3, 2]);
    });

    it('已经试过的地址下一轮不再要，同一张也只试一次', async () => {
        const it_ = rig(async (request) => {
            if (!request.cursor) {
                return {
                    photos: [photo('a')],
                    cursor: { mode: 'ordered', updateTime: new Date('2026-07-20'), id: 'a-id' },
                    exhausted: false,
                };
            }
            return {
                photos: [photo('a'), photo('b', 'b-key'), photo('c', 'c-key')],
                cursor: { mode: 'ordered', updateTime: new Date('2026-07-19'), id: 'c-id' },
                exhausted: true,
            };
        }, { upload: async () => null });

        const photos = await readyPhotos(it_.deps)(query({ page_size: 2 }));

        expect(photos.map((p) => p.pixiv_addr)).toEqual(['b', 'c']);
        expect(it_.asked[1]!.excluded).toContain('a');
        // 'a' 在第二页里又出现了一次，但下载只发生过一次。
        expect(it_.read).toEqual(['a.png']);
    });

    it('库里说没有更多了就停，哪怕还没凑够', async () => {
        const it_ = rig(async () => ({
            photos: [photo('a', 'a-key'), photo('b', 'b-key')],
            exhausted: true,
        }));

        const photos = await readyPhotos(it_.deps)(query({ page_size: 3 }));

        expect(photos.map((p) => p.pixiv_addr)).toEqual(['a', 'b']);
        expect(it_.asked).toHaveLength(1);
    });

    // 没有这条兜底就是死循环打库：库一直回空页、也不说自己到头了。
    it('既没试到新地址、游标也没动，就停下来（不死循环）', async () => {
        const it_ = rig(async () => ({ photos: [], exhausted: false }));

        const photos = await readyPhotos(it_.deps)(query({ page_size: 3 }));

        expect(photos).toEqual([]);
        expect(it_.asked).toHaveLength(1);
    });
});

describe('显式地址（「查看详情」那条路）', () => {
    it('按输入顺序交，去重，不在输入里的一律不要', async () => {
        const it_ = rig(async () => ({
            photos: [photo('a', 'a-key'), photo('outside', 'outside-key'), photo('b', 'b-key')],
            cursor: { mode: 'explicit', offset: 2 },
            exhausted: true,
        }));

        const photos = await readyPhotos(it_.deps)(
            query({ status: StatusMode.ALL, page_size: 3, pixiv_addrs: ['b', 'a', 'b'] }),
        );

        expect(photos.map((p) => p.pixiv_addr)).toEqual(['b', 'a']);
        expect(it_.asked).toHaveLength(1);
    });
});
