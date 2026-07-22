import { describe, expect, it, mock } from 'bun:test';
import { Readable } from 'stream';

import { StatusMode, type ImageForLark, type ListPixivImageDto } from 'types/pixiv';
import type { LocalPixivCandidatePage, LocalPixivCandidateRequest } from './local-source';
import { fetchUploadedImages, type FetchUploadedImagesDependencies } from './upload';

const orderedParams: ListPixivImageDto = {
    status: StatusMode.VISIBLE,
    page: 2,
    page_size: 2,
    random_mode: false,
};

function image(pixivAddr: string, imageKey?: string): ImageForLark {
    return {
        pixiv_addr: pixivAddr,
        image_key: imageKey,
        tos_file_name: imageKey ? undefined : `${pixivAddr}.png`,
    };
}

function dependencies(
    loadCandidates: (request: LocalPixivCandidateRequest) => Promise<LocalPixivCandidatePage>,
    overrides: Partial<FetchUploadedImagesDependencies> = {},
): FetchUploadedImagesDependencies {
    return {
        loadCandidates,
        readContent: async () => Buffer.from('image'),
        resize: async () => ({
            outFile: Readable.from(Buffer.from('image')),
            imgWidth: 100,
            imgHeight: 200,
        }),
        upload: async () => ({ image_key: 'uploaded-key' }),
        reportUpload: async () => undefined,
        ...overrides,
    };
}

describe('fetchUploadedImages candidate refill', () => {
    it('continues after a failed page-two candidate without applying the page offset again', async () => {
        const requests: LocalPixivCandidateRequest[] = [];
        const loadCandidates = mock(async (request: LocalPixivCandidateRequest) => {
            requests.push(request);
            if (!request.cursor) {
                return {
                    images: [image('c'), image('d', 'd-key')],
                    cursor: {
                        mode: 'ordered' as const,
                        updateTime: new Date('2026-07-20T00:00:00.000Z'),
                        id: 'd-id',
                    },
                    exhausted: false,
                };
            }
            return {
                images: [image('e', 'e-key')],
                cursor: {
                    mode: 'ordered' as const,
                    updateTime: new Date('2026-07-19T00:00:00.000Z'),
                    id: 'e-id',
                },
                exhausted: true,
            };
        });
        const readContent = mock(async (key: string) => {
            if (key === 'c.png') throw new Error('missing MinIO object');
            return Buffer.from('image');
        });

        const result = await fetchUploadedImages(
            orderedParams,
            dependencies(loadCandidates, { readContent }),
        );

        expect(result.map((item) => item.pixiv_addr)).toEqual(['d', 'e']);
        expect(requests).toHaveLength(2);
        expect(requests[0].cursor).toBeUndefined();
        expect(requests[1].cursor).toEqual({
            mode: 'ordered',
            updateTime: new Date('2026-07-20T00:00:00.000Z'),
            id: 'd-id',
        });
        expect(requests[1].params.page).toBe(2);
    });

    it('attempts a duplicate address only once while refilling from later candidates', async () => {
        const requests: LocalPixivCandidateRequest[] = [];
        const loadCandidates = mock(async (request: LocalPixivCandidateRequest) => {
            requests.push(request);
            if (requests.length === 1) {
                return {
                    images: [image('a')],
                    cursor: {
                        mode: 'ordered' as const,
                        updateTime: new Date('2026-07-20T00:00:00.000Z'),
                        id: 'a-id',
                    },
                    exhausted: false,
                };
            }
            return {
                images: [image('a'), image('b', 'b-key'), image('c', 'c-key')],
                cursor: {
                    mode: 'ordered' as const,
                    updateTime: new Date('2026-07-19T00:00:00.000Z'),
                    id: 'c-id',
                },
                exhausted: true,
            };
        });
        const upload = mock(async () => undefined);
        const readContent = mock(async () => Buffer.from('image'));

        const result = await fetchUploadedImages(
            { ...orderedParams, page: 1 },
            dependencies(loadCandidates, { upload, readContent }),
        );

        expect(result.map((item) => item.pixiv_addr)).toEqual(['b', 'c']);
        expect(readContent).toHaveBeenCalledTimes(1);
        expect(upload).toHaveBeenCalledTimes(1);
        expect(requests[1].excludedPixivAddrs).toContain('a');
    });

    it('returns the available random candidates when the unique address pool is exhausted', async () => {
        const loadCandidates = mock(async () => ({
            images: [image('a', 'a-key'), image('b', 'b-key')],
            exhausted: true,
        }));

        const result = await fetchUploadedImages(
            {
                status: StatusMode.VISIBLE,
                page: 1,
                page_size: 3,
                random_mode: true,
            },
            dependencies(loadCandidates),
        );

        expect(result.map((item) => item.pixiv_addr)).toEqual(['a', 'b']);
        expect(loadCandidates).toHaveBeenCalledTimes(1);
    });

    it('deduplicates explicit addresses, preserves input order, and never returns unrelated images', async () => {
        const loadCandidates = mock(async () => ({
            images: [image('a', 'a-key'), image('outside', 'outside-key'), image('b', 'b-key')],
            cursor: { mode: 'explicit' as const, offset: 2 },
            exhausted: true,
        }));

        const result = await fetchUploadedImages(
            {
                status: StatusMode.ALL,
                page: 1,
                page_size: 3,
                random_mode: false,
                pixiv_addrs: ['b', 'a', 'b'],
            },
            dependencies(loadCandidates),
        );

        expect(result.map((item) => item.pixiv_addr)).toEqual(['b', 'a']);
        expect(loadCandidates).toHaveBeenCalledTimes(1);
    });
});
