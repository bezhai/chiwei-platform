// 本地 pixiv 镜像：查询构造、候选页装配、对象名映射、回写 image_key。
//
// 这一层的错法都是**查回来的图不对**，而不是报错：过滤条件少一条 → 把已删除的图发进
// 群；分组排序反了 → 同一张图挑到没有 image_key 的那份、每次都重传一遍；显式地址的
// 顺序丢了 → 「查看详情」列出来的图跟卡片上那批对不上。所以断言直接钉在构造出来的
// 查询和交出去的那一页上。

import { describe, expect, it } from 'bun:test';
import type { Document } from 'mongodb';
import { StatusMode, type ImageForLark, type ListPixivImageDto } from '@inner/pixiv-client';

import {
    minioObjectName,
    photoFilter,
    photoOf,
    photoPipeline,
    pixivMirror,
    type PhotoDocuments,
    type PhotoObjectStore,
} from './pixiv-mirror';

function query(overrides: Partial<ListPixivImageDto> = {}): ListPixivImageDto {
    return {
        status: StatusMode.VISIBLE,
        page: 1,
        page_size: 6,
        random_mode: false,
        ...overrides,
    };
}

/** 一个只会回放固定文档的集合替身，顺带记下它收到的 pipeline。 */
function documents(docs: Document[]): PhotoDocuments & {
    pipelines: Document[][];
    updates: { filter: Document; update: Document }[];
} {
    const pipelines: Document[][] = [];
    const updates: { filter: Document; update: Document }[] = [];
    return {
        pipelines,
        updates,
        aggregate(pipeline) {
            pipelines.push(pipeline);
            return { toArray: async () => docs };
        },
        async updateMany(filter, update) {
            updates.push({ filter, update });
        },
    };
}

const noObjects: PhotoObjectStore = {
    read: async () => {
        throw new Error('this test does not read bytes');
    },
};

function mirror(docs: PhotoDocuments, objects: PhotoObjectStore = noObjects) {
    return pixivMirror({ documents: async () => docs, objects: async () => objects });
}

// ---------------------------------------------------------------------------

describe('查询构造：过滤条件', () => {
    // 每一条都对应一种"发错图"：没有 pixiv_addr 的文档拼不出详情按钮；两个 key 都空的
    // 文档既没有 image_key 也下载不到；状态过滤掉了才不会把删掉的图翻出来。
    it('永远先要求有地址、且至少有一个能取到图的 key', () => {
        const filter = photoFilter(query()) as { $and: Document[] };

        expect(filter.$and[0]).toEqual({
            pixiv_addr: { $type: 'string', $ne: '' },
            $or: [
                { image_key: { $type: 'string', $ne: '' } },
                { tos_file_name: { $type: 'string', $ne: '' } },
            ],
        });
    });

    it.each([
        [StatusMode.VISIBLE, { visible: true, del_flag: { $ne: true } }],
        [StatusMode.NOT_DELETE, { del_flag: { $ne: true } }],
        [StatusMode.DELETE, { del_flag: true }],
        [StatusMode.NO_VISIBLE, { visible: false, del_flag: { $ne: true } }],
    ])('status=%s 翻成它自己那条状态过滤', (status, expected) => {
        const filter = photoFilter(query({ status })) as { $and: Document[] };
        expect(filter.$and[1]).toEqual(expected);
    });

    // ALL 是「详情」那条路用的：卡片上列出来的图可能已经被删了，还得查得到。
    it('status=ALL 不加任何状态过滤', () => {
        const filter = photoFilter(query({ status: StatusMode.ALL })) as { $and: Document[] };
        expect(filter.$and).toHaveLength(1);
    });

    // 标签一条一个 $and：多个标签是"都要满足"，塞进一个 $or 会变成"满足任一"。
    it('每个标签各是一条 $and，五个字段任一命中即可，大小写不敏感', () => {
        const filter = photoFilter(query({ tag_and_author: ['刻晴', '原神'] })) as {
            $and: Array<{ $or?: Array<Record<string, RegExp>> }>;
        };

        expect(filter.$and).toHaveLength(4);
        const first = filter.$and[2]!.$or!;
        expect(Object.keys(first[0]!)).toEqual(['author']);
        expect(first.map((clause) => Object.keys(clause)[0])).toEqual([
            'author',
            'title',
            'multi_tags.name',
            'multi_tags.translation',
            'tagger_search_terms',
        ]);
        expect(first[0]!.author!.test('KEQING刻晴')).toBe(true);
        expect(first[0]!.author!.flags).toContain('i');
    });

    it('空白标签不产生条件（否则一个空格会把整库都匹配上）', () => {
        const filter = photoFilter(query({ tags: ['  ', ''] })) as { $and: Document[] };
        expect(filter.$and).toHaveLength(2);
    });

    // 标签里的正则元字符必须被转义，否则一个 `(` 会让整条查询抛。
    it('标签里的正则元字符按字面量处理', () => {
        const filter = photoFilter(query({ tags: ['a(b'] })) as {
            $and: Array<{ $or?: Array<Record<string, RegExp>> }>;
        };
        expect(filter.$and[2]!.$or![0]!.author!.test('a(b')).toBe(true);
    });

    it('tag_and_author 优先于 tags', () => {
        const filter = photoFilter(query({ tags: ['tags'], tag_and_author: ['both'] })) as {
            $and: Array<{ $or?: Array<Record<string, RegExp>> }>;
        };
        expect(filter.$and[2]!.$or![0]!.author!.test('both')).toBe(true);
        expect(filter.$and[2]!.$or![0]!.author!.test('tags')).toBe(false);
    });

    it('start_time 变成 create_time 的下界（「今日新图」靠它）', () => {
        const filter = photoFilter(
            query({ status: StatusMode.ALL, start_time: 1_700_000_000_000 }),
        ) as { $and: Document[] };
        expect(filter.$and[1]).toEqual({ create_time: { $gte: new Date(1_700_000_000_000) } });
    });

    it('显式地址去重之后进 $in', () => {
        const filter = photoFilter(
            query({ status: StatusMode.ALL, pixiv_addrs: ['b', 'a', 'b'] }),
        ) as { $and: Document[] };
        expect(filter.$and[1]).toEqual({ pixiv_addr: { $in: ['b', 'a'] } });
    });
});

describe('查询构造：pipeline', () => {
    // 同一个 pixiv_addr 在库里可能有多份文档（重复抓取），其中只有一份带 image_key。
    // 排序把带 key 的排到最前、分组只留第一份 —— 顺序反了就会每次都挑到没 key 的那份，
    // 于是每次发图都重新下载、重新上传同一张图。
    it('先按"有没有 image_key"排序，再按地址分组只留一份', () => {
        const pipeline = photoPipeline({ query: query(), limit: 2 });

        const sortAt = pipeline.findIndex((stage) => '$sort' in stage);
        const groupAt = pipeline.findIndex((stage) => '$group' in stage);
        expect(pipeline[sortAt]).toEqual({
            $sort: { pixiv_addr: 1, __has_image_key: -1, update_time: -1, _id: -1 },
        });
        expect(pipeline[groupAt]).toEqual({
            $group: { _id: '$pixiv_addr', __candidate: { $first: '$$ROOT' } },
        });
        expect(sortAt).toBeLessThan(groupAt);
    });

    it('random_mode 用 $sample，不翻页也不排序', () => {
        const pipeline = photoPipeline({ query: query({ random_mode: true }), limit: 3 });

        expect(pipeline).toContainEqual({ $sample: { size: 3 } });
        expect(pipeline.some((stage) => '$skip' in stage)).toBe(false);
    });

    // 续页只能靠游标：再套一次 $skip 会把上一页刚翻过去的那一段又跳一遍。
    it('首页按 page 算 $skip，续页改用游标条件且不再 $skip', () => {
        const params = query({ page: 2, page_size: 2 });
        const first = photoPipeline({ query: params, limit: 2 });
        const next = photoPipeline({
            query: params,
            limit: 1,
            cursor: { mode: 'ordered', updateTime: new Date('2026-07-20T00:00:00.000Z'), id: 'x' },
        });

        expect(first).toContainEqual({ $skip: 2 });
        expect(next.some((stage) => '$skip' in stage)).toBe(false);
        expect(JSON.stringify(next)).toContain('__candidate_update_time');
    });

    it('已经试过的地址被排除掉（随机模式下不会反复抽到同一张）', () => {
        const pipeline = photoPipeline({
            query: query({ random_mode: true }),
            limit: 2,
            excluded: ['a', 'b'],
        });

        expect(JSON.stringify(pipeline)).toContain('$nin');
        expect(JSON.stringify(pipeline)).toContain('a');
    });

    it('显式地址那条路按窗口大小截断，不用 $sample 也不用 $skip', () => {
        const pipeline = photoPipeline({
            query: query({ pixiv_addrs: ['a', 'b', 'c'], random_mode: true }),
            limit: 2,
        });

        expect(pipeline).toContainEqual({ $limit: 2 });
        expect(pipeline.some((stage) => '$sample' in stage)).toBe(false);
        expect(pipeline.some((stage) => '$skip' in stage)).toBe(false);
    });

    it('显式地址配上非显式游标就抛（两种翻页方式混用会静默跳错段）', () => {
        expect(() =>
            photoPipeline({
                query: query({ pixiv_addrs: ['a'] }),
                limit: 1,
                cursor: { mode: 'ordered', updateTime: new Date(), id: 'x' },
            }),
        ).toThrow();
    });

    it('顺序翻页配上显式游标也抛', () => {
        expect(() =>
            photoPipeline({ query: query(), limit: 1, cursor: { mode: 'explicit', offset: 1 } }),
        ).toThrow();
    });
});

describe('候选页', () => {
    it('按地址去重之后交出来，游标指向最后一份文档', async () => {
        const docs = documents([
            { _id: '2', pixiv_addr: 'a', image_key: 'a-key', update_time: new Date('2026-07-20') },
            { _id: '1', pixiv_addr: 'a', image_key: 'a-key-2' },
            { _id: '0', pixiv_addr: 'b', image_key: 'b-key', __candidate_update_time: new Date('2026-07-19') },
        ]);

        const page = await mirror(docs).candidates({ query: query(), limit: 6 });

        expect(page.photos.map((photo) => photo.pixiv_addr)).toEqual(['a', 'b']);
        expect(page.cursor).toEqual({
            mode: 'ordered',
            updateTime: new Date('2026-07-19'),
            id: '0',
        });
        // 拿回来的比要的少 = 库里没有更多了。
        expect(page.exhausted).toBe(true);
    });

    // 随机模式没有稳定顺序，游标毫无意义 —— 交出去只会让调用方以为还能续。
    it('随机模式不给游标', async () => {
        const page = await mirror(
            documents([{ _id: '1', pixiv_addr: 'a', image_key: 'a-key' }]),
        ).candidates({ query: query({ random_mode: true }), limit: 1 });

        expect(page.cursor).toBeUndefined();
        expect(page.exhausted).toBe(false);
    });

    // 「查看详情」按卡片上那批地址查，交回来的顺序必须跟卡片一致，多出来的一律不要。
    it('显式地址：按输入顺序交，不在输入里的丢掉，游标是下一个偏移', async () => {
        const docs = documents([
            { _id: '3', pixiv_addr: 'outside', image_key: 'outside-key' },
            { _id: '2', pixiv_addr: 'a', image_key: 'a-key' },
            { _id: '1', pixiv_addr: 'b', image_key: 'b-key' },
        ]);

        const page = await mirror(docs).candidates({
            query: query({ pixiv_addrs: ['b', 'a', 'b'], status: StatusMode.ALL }),
            limit: 6,
        });

        expect(page.photos.map((photo) => photo.pixiv_addr)).toEqual(['b', 'a']);
        expect(page.cursor).toEqual({ mode: 'explicit', offset: 2 });
        expect(page.exhausted).toBe(true);
        expect(JSON.stringify(docs.pipelines[0])).not.toContain('outside');
    });

    // 窗口已经翻到头：一次库都不用查。
    it('显式地址翻过头时直接交空页，不查库', async () => {
        const docs = documents([]);

        const page = await mirror(docs).candidates({
            query: query({ pixiv_addrs: ['a'], page: 3, page_size: 2 }),
            limit: 2,
        });

        expect(page.photos).toEqual([]);
        expect(page.exhausted).toBe(true);
        expect(docs.pipelines).toEqual([]);
    });
});

describe('取字节与回写', () => {
    // MinIO 里的对象名只有 basename，而库里存的是带前缀的路径。映射错了就是 404。
    it('对象名取路径的最后一段', () => {
        expect(minioObjectName('pixiv_img_v2/20260604/12345678_p0.png')).toBe('12345678_p0.png');
        expect(minioObjectName('12345678_p0.png')).toBe('12345678_p0.png');
    });

    it('bytes 按对象名去对象存储取', async () => {
        const read: string[] = [];
        const objects: PhotoObjectStore = {
            read: async (name) => {
                read.push(name);
                return Buffer.from('bytes');
            },
        };

        const bytes = await mirror(documents([]), objects).bytes('dir/one.png');

        expect(bytes.toString()).toBe('bytes');
        expect(read).toEqual(['one.png']);
    });

    // 回写覆盖同一地址的**每一份**文档：只更一份的话，下次分组挑到另一份又会重传。
    it('回写 image_key 打在同一地址的所有文档上，并刷新 update_time', async () => {
        const docs = documents([]);

        await mirror(docs).noteLarkImageKey({
            pixiv_addr: 'a',
            image_key: 'img_1',
            width: 100,
            height: 200,
        });

        expect(docs.updates[0]!.filter).toEqual({ pixiv_addr: 'a' });
        const set = (docs.updates[0]!.update as { $set: Record<string, unknown> }).$set;
        expect(set.image_key).toBe('img_1');
        expect(set.width).toBe(100);
        expect(set.height).toBe(200);
        expect(set.update_time).toBeInstanceOf(Date);
    });
});

describe('文档映射', () => {
    it('按字段名逐个取，缺的留空', () => {
        expect(
            photoOf({
                pixiv_addr: '12345678_p0.png',
                author: 'author',
                image_key: 'img_key',
                tos_file_name: 'pixiv_img_v2/20260604/12345678_p0.png',
                width: 800,
                height: 1200,
                multi_tags: [{ name: 'keqing', translation: '刻晴', visible: true }],
            }),
        ).toEqual({
            pixiv_addr: '12345678_p0.png',
            author: 'author',
            image_key: 'img_key',
            tos_file_name: 'pixiv_img_v2/20260604/12345678_p0.png',
            width: 800,
            height: 1200,
            multi_tags: [{ name: 'keqing', translation: '刻晴', visible: true }],
        });
    });

    // 空串等于没有：image_key 是空串时下游会把它当成"已经传过了"，然后拿一个空 key
    // 去拼卡片，飞书显示成一个裂开的图。
    it('空串一律当成没有', () => {
        const photo = photoOf({ pixiv_addr: 'a', image_key: '', author: '', width: Number.NaN });
        expect(photo).toEqual({
            pixiv_addr: 'a',
            author: undefined,
            image_key: undefined,
            width: undefined,
            height: undefined,
            multi_tags: undefined,
            tos_file_name: undefined,
        } as ImageForLark);
    });

    it('没有地址的文档不是一张图', () => {
        expect(photoOf({ image_key: 'k' })).toBeNull();
    });
});
