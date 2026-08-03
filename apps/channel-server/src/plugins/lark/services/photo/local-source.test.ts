import { describe, expect, it, mock } from 'bun:test';

import { StatusMode } from 'types/pixiv';
import {
    buildLocalPixivCandidatePipeline,
    buildLocalPixivImageFilter,
    dedupePixivAddrs,
    getLocalPixivImageCandidates,
    mapLocalPixivImageDoc,
    minioObjectName,
} from './local-source';

describe('local pixiv image source', () => {
    it('builds a visible AND tag_or_author Mongo filter for 发图 tags', () => {
        const filter = buildLocalPixivImageFilter({
            status: StatusMode.VISIBLE,
            page: 1,
            page_size: 6,
            random_mode: true,
            tag_and_author: ['刻晴'],
        }) as any;

        expect(filter.$and[0]).toEqual({
            pixiv_addr: { $type: 'string', $ne: '' },
            $or: [
                { image_key: { $type: 'string', $ne: '' } },
                { tos_file_name: { $type: 'string', $ne: '' } },
            ],
        });
        expect(filter.$and[1]).toEqual({ visible: true, del_flag: { $ne: true } });
        expect(filter.$and[2].$or).toHaveLength(5);
        expect(filter.$and[2].$or[0].author.test('刻晴')).toBe(true);
        expect(filter.$and[2].$or[2]['multi_tags.name'].test('刻晴')).toBe(true);
        expect(filter.$and[2].$or[4].tagger_search_terms.test('刻晴')).toBe(true);
    });

    it('deduplicates explicit addresses by first occurrence and drops empty values', () => {
        expect(dedupePixivAddrs(['b.png', 'a.png', 'b.png', '', 'c.png', 'a.png'])).toEqual([
            'b.png',
            'a.png',
            'c.png',
        ]);
    });

    it('uses the requested page offset only before an ordered continuation', () => {
        const params = {
            status: StatusMode.VISIBLE,
            page: 2,
            page_size: 2,
            random_mode: false,
        };
        const initial = buildLocalPixivCandidatePipeline({ params, limit: 2 });
        const continued = buildLocalPixivCandidatePipeline({
            params,
            limit: 1,
            cursor: {
                mode: 'ordered',
                updateTime: new Date('2026-07-20T00:00:00.000Z'),
                id: 'cursor-id',
            },
        });

        expect(initial.some((stage) => '$skip' in stage && stage.$skip === 2)).toBe(true);
        expect(continued.some((stage) => '$skip' in stage)).toBe(false);
        expect(
            continued.some(
                (stage) =>
                    '$match' in stage &&
                    Array.isArray(stage.$match?.$or) &&
                    stage.$match.$or.some((item: Record<string, unknown>) =>
                        Object.hasOwn(item, '__candidate_update_time'),
                    ),
            ),
        ).toBe(true);
    });

    it('groups duplicate documents by address after preferring an existing image key', () => {
        const pipeline = buildLocalPixivCandidatePipeline({
            params: {
                status: StatusMode.VISIBLE,
                page: 1,
                page_size: 2,
                random_mode: false,
            },
            limit: 2,
        });

        expect(pipeline).toContainEqual({
            $sort: {
                pixiv_addr: 1,
                __has_image_key: -1,
                update_time: -1,
                _id: -1,
            },
        });
        expect(pipeline).toContainEqual({
            $group: { _id: '$pixiv_addr', __candidate: { $first: '$$ROOT' } },
        });
    });

    it('samples random candidates without replacement by excluding attempted addresses', () => {
        const pipeline = buildLocalPixivCandidatePipeline({
            params: {
                status: StatusMode.VISIBLE,
                page: 3,
                page_size: 2,
                random_mode: true,
            },
            limit: 2,
            excludedPixivAddrs: ['a.png', 'b.png'],
        });

        expect(pipeline.some((stage) => '$sample' in stage)).toBe(true);
        expect(pipeline.some((stage) => '$skip' in stage)).toBe(false);
        expect(JSON.stringify(pipeline)).toContain('$nin');
        expect(JSON.stringify(pipeline)).toContain('a.png');
    });

    it('keeps explicit candidates inside the deduplicated input set and in input order', async () => {
        const aggregate = mock((_pipeline: Record<string, unknown>[]) => ({
            toArray: async () => [
                { _id: '3', pixiv_addr: 'outside.png', image_key: 'outside-key' },
                { _id: '2', pixiv_addr: 'a.png', image_key: 'a-key' },
                { _id: '1', pixiv_addr: 'b.png', image_key: 'b-key' },
            ],
        }));

        const page = await getLocalPixivImageCandidates(
            {
                params: {
                    status: StatusMode.ALL,
                    page: 1,
                    page_size: 6,
                    random_mode: false,
                    pixiv_addrs: ['b.png', 'a.png', 'b.png'],
                },
                limit: 6,
            },
            { collection: { aggregate } },
        );

        expect(page.images.map((image) => image.pixiv_addr)).toEqual(['b.png', 'a.png']);
        expect(page.exhausted).toBe(true);
        expect(page.cursor).toEqual({ mode: 'explicit', offset: 2 });
        const pipeline = aggregate.mock.calls[0][0] as Record<string, any>[];
        expect(JSON.stringify(pipeline)).not.toContain('outside.png');
        expect(JSON.stringify(pipeline)).toContain('b.png');
        expect(JSON.stringify(pipeline)).toContain('a.png');
    });

    it('uses basename as the local MinIO object name', () => {
        expect(minioObjectName('pixiv_img_v2/20260604/12345678_p0.png')).toBe('12345678_p0.png');
        expect(minioObjectName('12345678_p0.png')).toBe('12345678_p0.png');
    });

    it('maps local Mongo docs to ImageForLark fields', () => {
        expect(
            mapLocalPixivImageDoc({
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
});
