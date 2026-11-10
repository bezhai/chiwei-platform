import { describe, expect, it } from 'bun:test';
import type { Document } from 'mongodb';
import { MongoCollection } from '@inner/shared/mongo';
import { bindPixivCollections } from './client';
import {
    addImage,
    checkExistPixivImg,
    findImageByPixivAddr,
    getAuthorLastDownloadAt,
    getMaxIllustId,
    insertDownloadTask,
    recordAuthorDownload,
    searchAndAddTranslate,
    buildClaimableTaskFilter,
    buildClaimUpdate,
    buildCompletionFilter,
    buildExhaustedReclaimFilter,
    buildDeadLetterUpdate,
    buildCompletedPixivImageFilter,
    buildImageByPixivAddrFilter,
} from './service';
import { DownloadTask, DownloadTaskStatus } from './types';

// Store functions take their collections as an optional last argument, so these
// tests bind the real MongoCollection wrapper over a fake driver collection instead
// of using mock.module (which pollutes sibling tests in the same bun run).
describe('buildImageByPixivAddrFilter', () => {
    it('empty pixivAddr -> null (caller short-circuits to null doc)', () => {
        expect(buildImageByPixivAddrFilter('')).toBeNull();
    });

    it('non-empty pixivAddr -> matches pixiv_addr AND requires a non-empty tos_file_name', () => {
        const filter = buildImageByPixivAddrFilter('123_p0.png');

        expect(filter).not.toBeNull();
        expect(filter!.pixiv_addr).toBe('123_p0.png');

        // a doc whose tos_file_name is missing or empty must NOT match this filter,
        // so the $nin guard against null/'' must be present
        expect(filter!.tos_file_name).toEqual({ $nin: [null, ''] } as any);
    });
});

describe('buildCompletedPixivImageFilter', () => {
    it('requires addImage metadata markers as well as a durable OSS key', () => {
        expect(buildCompletedPixivImageFilter('123_p0.png')).toEqual({
            pixiv_addr: '123_p0.png',
            tos_file_name: { $nin: [null, ''] },
            illust_id: { $gt: 0 },
            multi_tags: { $type: 'array' },
            create_time: { $type: 'date' },
        } as any);
    });

    it('rejects an empty address before querying Mongo', () => {
        expect(buildCompletedPixivImageFilter('')).toBeNull();
    });
});

describe('buildClaimableTaskFilter', () => {
    const now = new Date('2026-07-06T12:00:00.000Z');
    const reclaimMs = 90 * 60 * 1000;

    it('claims Pending and Fail unconditionally', () => {
        const filter = buildClaimableTaskFilter(now, reclaimMs);

        expect(filter.$or?.[0]).toEqual({
            status: { $in: [DownloadTaskStatus.Pending, DownloadTaskStatus.Fail] },
        });
    });

    it('claims Running only when last_run_time is older than the reclaim threshold', () => {
        const filter = buildClaimableTaskFilter(now, reclaimMs);

        expect(filter.$or?.[1]).toEqual({
            status: DownloadTaskStatus.Running,
            last_run_time: { $lt: new Date('2026-07-06T10:30:00.000Z') },
        });
    });

    it('has no other branch, so fresh Running / Success / Dead can never match', () => {
        const filter = buildClaimableTaskFilter(now, reclaimMs);

        expect(Object.keys(filter)).toEqual(['$or']);
        expect(filter.$or).toHaveLength(2);
    });
});

describe('buildClaimUpdate', () => {
    it('marks Running, stamps a new generation (last_run_time) and consumes retry budget atomically', () => {
        const now = new Date('2026-07-06T12:00:00.000Z');

        expect(buildClaimUpdate(now)).toEqual({
            $set: {
                status: DownloadTaskStatus.Running,
                last_run_time: now,
                update_time: now,
                last_run_error: '',
            },
            $inc: { retry_time: 1 },
        });
    });
});

// A poison task (one that hangs the consumer every time) never reaches the
// fail() completion path, so retry_time keeps growing via claim $inc while the
// task bounces between Running and reclaim forever. The dead-letter sweep must
// catch exactly these: reclaim-eligible Running docs whose retry budget is gone.
describe('buildExhaustedReclaimFilter', () => {
    const now = new Date('2026-07-06T12:00:00.000Z');
    const reclaimMs = 90 * 60 * 1000;

    it('matches only stale Running docs whose retry budget is exhausted', () => {
        expect(buildExhaustedReclaimFilter(now, reclaimMs)).toEqual({
            status: DownloadTaskStatus.Running,
            last_run_time: { $lt: new Date('2026-07-06T10:30:00.000Z') },
            retry_time: { $gte: DownloadTask.MaxRetryTime },
        } as any);
    });

    it('never matches docs that still have retry budget (they go back through reclaim)', () => {
        const filter = buildExhaustedReclaimFilter(now, reclaimMs) as any;

        expect(filter.retry_time.$gte).toBe(3);
    });
});

describe('buildDeadLetterUpdate', () => {
    // returns bare fields (NOT an {$set:...} document): MongoCollection.updateMany
    // wraps its update argument in $set itself, so a pre-wrapped document would
    // become {$set:{$set:...}} and fail at runtime
    it('marks Dead with an explanation so operators can tell sweep from download failure', () => {
        const now = new Date('2026-07-06T12:00:00.000Z');
        const update = buildDeadLetterUpdate(now) as any;

        expect(update.$set).toBeUndefined();
        expect(update.status).toBe(DownloadTaskStatus.Dead);
        expect(update.update_time).toEqual(now);
        expect(update.last_run_error).toContain('reclaim');
    });
});

describe('buildCompletionFilter', () => {
    it('pins illust_id AND the claimed generation, so a doc re-claimed later (new last_run_time) does not match', () => {
        const claimedRunTime = new Date('2026-07-06T12:00:00.000Z');

        expect(buildCompletionFilter('123', claimedRunTime)).toEqual({
            illust_id: '123',
            last_run_time: claimedRunTime,
        } as any);
    });

    it('undefined generation only matches docs with null/missing last_run_time (never a claimed doc)', () => {
        expect(buildCompletionFilter('123', undefined)).toEqual({
            illust_id: '123',
            last_run_time: null,
        } as any);
    });
});

interface DriverCall {
    collection: string;
    method: string;
    args: unknown[];
}

/**
 * Binds the real collection roles over driver-level fakes that record every call.
 * `results[collection][method]` is what the fake driver returns; `find` gets wrapped
 * in a cursor like the real driver does.
 */
function recordingCollections(results: Record<string, Record<string, unknown>> = {}) {
    const calls: DriverCall[] = [];
    const service = {
        getCollection(name: string) {
            const driver = new Proxy({}, {
                get: (_target, method: string) => (...args: unknown[]) => {
                    calls.push({ collection: name, method, args });
                    const result = results[name]?.[method];
                    if (method === 'find') return { toArray: async () => result ?? [] };
                    return Promise.resolve(result ?? null);
                },
            });
            return new MongoCollection(driver as any);
        },
    };
    return { calls, collections: bindPixivCollections(service as any) };
}

describe('catalog reads go to pixiv_images', () => {
    it('getMaxIllustId finds the newest known illust among the candidates', async () => {
        const { calls, collections } = recordingCollections({ pixiv_images: { find: [{ illust_id: 102 }] } });

        await expect(getMaxIllustId([101, 102, 103], collections)).resolves.toBe(102);
        expect(calls).toEqual([{
            collection: 'pixiv_images',
            method: 'find',
            args: [{ illust_id: { $in: [101, 102, 103] } }, { limit: 1, sort: { illust_id: -1 } }],
        }]);
    });

    it('getMaxIllustId returns 0 on an empty catalog', async () => {
        const { collections } = recordingCollections();

        await expect(getMaxIllustId([101], collections)).resolves.toBe(0);
    });

    it('checkExistPixivImg dedups a page against completed catalog records', async () => {
        const { calls, collections } = recordingCollections({ pixiv_images: { countDocuments: 1 } });

        await expect(checkExistPixivImg('123_p0.png', collections)).resolves.toBe(true);
        expect(calls).toEqual([{
            collection: 'pixiv_images',
            method: 'countDocuments',
            args: [buildCompletedPixivImageFilter('123_p0.png'), undefined],
        }]);
    });

    it('findImageByPixivAddr reads the OSS key for MinIO sync from the catalog', async () => {
        const doc = { pixiv_addr: '123_p0.png', tos_file_name: 'pixiv_img_v2/20261110/123_p0.png' };
        const { calls, collections } = recordingCollections({ pixiv_images: { findOne: doc } });

        await expect(findImageByPixivAddr('123_p0.png', collections)).resolves.toEqual(doc as any);
        expect(calls.map(({ collection, method }) => `${collection}.${method}`)).toEqual(['pixiv_images.findOne']);
    });
});

describe('queue, translations and author throttle live in chiwei_pixiv', () => {
    it('insertDownloadTask dedups and enqueues in pixiv_download_tasks', async () => {
        const { calls, collections } = recordingCollections();

        await expect(insertDownloadTask('101', new AbortController().signal, collections)).resolves.toBe(true);
        expect(calls.map(({ collection, method }) => `${collection}.${method}`)).toEqual([
            'pixiv_download_tasks.find',
            'pixiv_download_tasks.insertOne',
        ]);
    });

    it('searchAndAddTranslate reads pixiv_tag_translations', async () => {
        const { calls, collections } = recordingCollections({
            pixiv_tag_translations: { findOne: { origin: '猫', translation: 'cat', has_translate: true } },
        });

        await expect(searchAndAddTranslate('猫', 'cat', '猫', collections)).resolves.toBe('cat');
        expect(calls).toEqual([{
            collection: 'pixiv_tag_translations',
            method: 'findOne',
            args: [{ origin: '猫', has_translate: true }, undefined],
        }]);
    });

    it('getAuthorLastDownloadAt reads unix seconds keyed by author id', async () => {
        const { calls, collections } = recordingCollections({
            pixiv_author_downloads: { findOne: { _id: '42', last_download_at: 1794283200 } },
        });

        await expect(getAuthorLastDownloadAt('42', collections)).resolves.toBe(1794283200);
        expect(calls).toEqual([{
            collection: 'pixiv_author_downloads',
            method: 'findOne',
            args: [{ _id: '42' }, undefined],
        }]);
    });

    it('getAuthorLastDownloadAt returns null for an author never downloaded', async () => {
        const { collections } = recordingCollections();

        await expect(getAuthorLastDownloadAt('42', collections)).resolves.toBeNull();
    });

    it('recordAuthorDownload upserts unix seconds, not milliseconds', async () => {
        const { calls, collections } = recordingCollections();

        await recordAuthorDownload('42', new Date('2026-11-10T04:00:00.500Z'), collections);

        expect(calls).toEqual([{
            collection: 'pixiv_author_downloads',
            method: 'updateOne',
            args: [{ _id: '42' }, { $set: { last_download_at: 1794283200 } }, { upsert: true }],
        }]);
    });
});

/**
 * A pixiv_images driver fake with just enough upsert semantics: plain-equality
 * filters, $set on every match, $setOnInsert + $set on insert. Any other operator
 * throws, so an implementation cannot pass by writing something the fake ignores.
 */
function catalogWithDocs(initial: Document[]) {
    const docs = initial.map((doc) => ({ ...doc }));
    const driver = {
        async updateMany(filter: Document, update: Document, options?: { upsert?: boolean }) {
            for (const key of Object.keys(update)) {
                if (key !== '$set' && key !== '$setOnInsert') throw new Error(`fake does not support ${key}`);
            }
            for (const value of Object.values(filter)) {
                if (value !== null && typeof value === 'object') throw new Error('fake supports equality filters only');
            }
            const matches = docs.filter((doc) => Object.entries(filter).every(([k, v]) => doc[k] === v));
            if (matches.length === 0 && options?.upsert) {
                docs.push({ ...filter, ...update.$setOnInsert, ...update.$set });
                return { matchedCount: 0, upsertedCount: 1 };
            }
            for (const doc of matches) Object.assign(doc, update.$set);
            return { matchedCount: matches.length, upsertedCount: 0 };
        },
    };
    const service = {
        getCollection: (name: string) => {
            if (name !== 'pixiv_images') return new MongoCollection({} as any);
            return new MongoCollection(driver as any);
        },
    };
    return { docs, collections: bindPixivCollections(service as any) };
}

const DOWNLOADED = {
    pixiv_name: '123_p0.png',
    tos_file_name: 'pixiv_img_v2/20261110/123_p0.png',
    need_download: true,
    author: 'artist',
    author_id: '42',
    is_r18: false,
    title: 'title',
};

describe('addImage writes the catalog record with the server-returned OSS key', () => {
    it('inserts a complete record when the catalog has no row for the page', async () => {
        const { docs, collections } = catalogWithDocs([]);

        await addImage([{ name: '猫', translation: 'cat', visible: false }], DOWNLOADED, collections);

        expect(docs).toHaveLength(1);
        expect(docs[0]).toMatchObject({
            pixiv_addr: '123_p0.png',
            tos_file_name: 'pixiv_img_v2/20261110/123_p0.png',
            multi_tags: [{ name: '猫', translation: 'cat', visible: true }],
            visible: true,
            author: 'artist',
            author_id: '42',
            illust_id: 123,
            title: 'title',
            need_download: true,
            del_flag: false,
        });
        expect(docs[0].create_time).toBeInstanceOf(Date);
        expect(docs[0].update_time).toBeInstanceOf(Date);
        // the inserted row is what checkExistPixivImg treats as completed
        expect(docs[0].illust_id).toBeGreaterThan(0);
    });

    it('keeps fields owned by lark-service and the tagger projection on every duplicate row', async () => {
        const larkUpdatedAt = new Date('2026-11-01T00:00:00.000Z');
        const taggerOwned = {
            tagger_result: { id: '123_p0.png' },
            tagger_search_terms: ['cat'],
            tagger_task_id: 'task-1',
            tagger_generation: 2,
            tagger_status: 'completed',
            tagger_updated_at: new Date('2026-11-02T00:00:00.000Z'),
        };
        const larkOwned = { image_key: 'img_v3_x', width: 800, height: 600, update_time: larkUpdatedAt };
        const { docs, collections } = catalogWithDocs([
            { _id: 'a', pixiv_addr: '123_p0.png', ...larkOwned, ...taggerOwned },
            { _id: 'b', pixiv_addr: '123_p0.png', ...larkOwned },
            { _id: 'c', pixiv_addr: '999_p0.png', title: 'other' },
        ]);

        await addImage([], { ...DOWNLOADED, is_r18: true }, collections);

        // toEqual, not toMatchObject: bun's toMatchObject does not compare Date values.
        const pick = (doc: Document, fields: Document) =>
            Object.fromEntries(Object.keys(fields).map((field) => [field, doc[field]]));
        expect(pick(docs[0], { ...larkOwned, ...taggerOwned })).toEqual({ ...larkOwned, ...taggerOwned });
        expect(pick(docs[1], larkOwned)).toEqual(larkOwned);
        expect(docs[0]).toMatchObject({
            tos_file_name: 'pixiv_img_v2/20261110/123_p0.png',
            visible: false,
            illust_id: 123,
        });
        expect(docs[1].tos_file_name).toBe('pixiv_img_v2/20261110/123_p0.png');
        expect(docs[2]).toEqual({ _id: 'c', pixiv_addr: '999_p0.png', title: 'other' });
    });
});
