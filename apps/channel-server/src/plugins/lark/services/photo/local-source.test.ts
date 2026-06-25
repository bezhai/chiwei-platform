import { afterEach, describe, expect, it } from 'bun:test';

import { StatusMode } from 'types/pixiv';
import {
    buildLocalPixivImageFilter,
    mapLocalPixivImageDoc,
    minioObjectName,
    shouldUseLocalPixivImageSource,
} from './local-source';

describe('local pixiv image source', () => {
    const originalSource = process.env.PIXIV_IMAGE_SOURCE;

    afterEach(() => {
        if (originalSource === undefined) {
            delete process.env.PIXIV_IMAGE_SOURCE;
        } else {
            process.env.PIXIV_IMAGE_SOURCE = originalSource;
        }
    });

    it('is enabled only by PIXIV_IMAGE_SOURCE=local', () => {
        process.env.PIXIV_IMAGE_SOURCE = 'local';
        expect(shouldUseLocalPixivImageSource()).toBe(true);

        process.env.PIXIV_IMAGE_SOURCE = 'remote';
        expect(shouldUseLocalPixivImageSource()).toBe(false);
    });

    it('builds a visible AND tag_or_author Mongo filter for 发图 tags', () => {
        const filter = buildLocalPixivImageFilter({
            status: StatusMode.VISIBLE,
            page: 1,
            page_size: 6,
            random_mode: true,
            tag_and_author: ['刻晴'],
        }) as any;

        expect(filter.$and[0]).toEqual({ visible: true, del_flag: { $ne: true } });
        expect(filter.$and[1].$or).toHaveLength(4);
        expect(filter.$and[1].$or[0].author.test('刻晴')).toBe(true);
        expect(filter.$and[1].$or[2]['multi_tags.name'].test('刻晴')).toBe(true);
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
