import { describe, expect, it } from 'bun:test';
import { ObjectId, type Document } from 'mongodb';
import { DownloadTaskStatus } from '../mongo/types';
import { listRecentRuns, runCloudImport, type CloudSource, type ScanCollection } from './importer';
import { MemoryCollection, MemoryRedis } from './memoryStores';

const NOW = new Date('2026-11-20T04:00:00.000Z');

function oid(n: number): ObjectId {
    return new ObjectId(n.toString(16).padStart(24, '0'));
}

interface CloudData {
    imgMap?: Document[];
    downloadTask?: Document[];
    transMap?: Document[];
    authorDict?: Record<string, string>;
}

function cloud(data: CloudData = {}) {
    const source: CloudSource = {
        imgMap: new MemoryCollection('img_map', data.imgMap),
        downloadTask: new MemoryCollection('download_task', data.downloadTask),
        transMap: new MemoryCollection('trans_map', data.transMap),
        redis: new MemoryRedis({ hashes: { download_user_dict: data.authorDict ?? {} } }),
    };
    const state = { opened: 0, closed: 0 };
    return {
        source,
        state,
        openSource: async () => {
            state.opened += 1;
            return {
                source,
                close: async () => {
                    state.closed += 1;
                },
            };
        },
    };
}

interface ClusterData {
    images?: Document[];
    downloadTasks?: Document[];
    tagTranslations?: Document[];
    authorDownloads?: Document[];
}

function cluster(data: ClusterData = {}) {
    return {
        target: {
            images: new MemoryCollection('pixiv_images', data.images),
            downloadTasks: new MemoryCollection('pixiv_download_tasks', data.downloadTasks),
            tagTranslations: new MemoryCollection('pixiv_tag_translations', data.tagTranslations),
            authorDownloads: new MemoryCollection('pixiv_author_downloads', data.authorDownloads),
        },
        runLog: new MemoryCollection('pixiv_cloud_import_runs'),
    };
}

type Cloud = ReturnType<typeof cloud>;
type Cluster = ReturnType<typeof cluster>;

function runImport(src: Cloud, dst: Cluster, overrides: Partial<Parameters<typeof runCloudImport>[0]> = {}) {
    return runCloudImport({
        runId: 'run-1',
        openSource: src.openSource,
        target: dst.target,
        runLog: dst.runLog,
        batchSize: 2,
        now: () => NOW,
        ...overrides,
    });
}

function doneEvent(dst: Cluster, source: string, runId = 'run-1'): Document {
    const event = dst.runLog
        .snapshot()
        .find((e) => e.run_id === runId && e.event === 'collection_done' && e.source === source);
    if (!event) {
        throw new Error(`no collection_done event for ${source} in ${runId}`);
    }
    return event;
}

function byId(docs: Document[]): Document[] {
    return [...docs].sort((a, b) => String(a._id).localeCompare(String(b._id)));
}

describe('runCloudImport: img_map -> pixiv_images', () => {
    it('merges by _id without overwriting fields owned by lark-service or the tagger', async () => {
        const clusterTime = new Date('2026-11-15T00:00:00.000Z');
        const cloudTime = new Date('2026-01-01T00:00:00.000Z');
        const dst = cluster({
            images: [{
                _id: oid(1),
                pixiv_addr: '100_p0.jpg',
                tos_file_name: 'pixiv_img_v2/20250101/100_p0.jpg',
                author: 'old name',
                image_key: 'img_v3_cluster',
                width: 800,
                height: 600,
                update_time: clusterTime,
                tagger_result: { rating: 'general' },
                tagger_search_terms: ['cat'],
                tagger_task_id: 'task-cluster',
                tagger_generation: 2,
                tagger_status: 'projected',
                tagger_updated_at: clusterTime,
            }],
        });
        const src = cloud({
            imgMap: [{
                _id: oid(1),
                pixiv_addr: '100_p0.jpg',
                tos_file_name: 'pixiv_img_v2/20250102/100_p0.jpg',
                author: 'new name',
                multi_tags: [{ name: '猫', translation: 'cat', visible: true }],
                visible: true,
                image_key: 'img_v3_cloud_stale',
                width: 1,
                height: 2,
                update_time: cloudTime,
                tagger_result: { stale: true },
                tagger_search_terms: ['stale'],
                tagger_task_id: 'task-cloud',
                tagger_generation: 0,
                tagger_status: 'stale',
                tagger_updated_at: cloudTime,
            }],
        });

        await runImport(src, dst);

        expect(dst.target.images.snapshot()).toEqual([{
            _id: oid(1),
            pixiv_addr: '100_p0.jpg',
            tos_file_name: 'pixiv_img_v2/20250102/100_p0.jpg',
            author: 'new name',
            multi_tags: [{ name: '猫', translation: 'cat', visible: true }],
            visible: true,
            image_key: 'img_v3_cluster',
            width: 800,
            height: 600,
            update_time: clusterTime,
            tagger_result: { rating: 'general' },
            tagger_search_terms: ['cat'],
            tagger_task_id: 'task-cluster',
            tagger_generation: 2,
            tagger_status: 'projected',
            tagger_updated_at: clusterTime,
        }]);
        expect(doneEvent(dst, 'img_map')).toMatchObject({ inserted: 0, merged: 1, skipped: {} });
    });

    it('inserts images the cluster does not have, keeping their _id and every field', async () => {
        const doc = {
            _id: oid(2),
            pixiv_addr: '200_p0.png',
            tos_file_name: 'pixiv_img_v2/20250103/200_p0.png',
            image_key: 'img_v3_cloud',
            width: 1000,
            height: 1400,
            update_time: new Date('2025-01-03T00:00:00.000Z'),
            illust_id: 200,
            visible: false,
        };
        const dst = cluster();

        await runImport(cloud({ imgMap: [doc] }), dst);

        expect(dst.target.images.snapshot()).toEqual([doc]);
        expect(doneEvent(dst, 'img_map')).toMatchObject({ inserted: 1, merged: 0 });
    });

    it('skips a cloud image whose pixiv_addr the cluster already holds under another _id', async () => {
        const workerDoc = {
            _id: oid(900),
            pixiv_addr: '300_p0.jpg',
            tos_file_name: 'pixiv_img_v2/20261120/300_p0.jpg',
            image_key: 'img_v3_worker',
        };
        const dst = cluster({ images: [workerDoc] });
        const src = cloud({
            imgMap: [{ _id: oid(3), pixiv_addr: '300_p0.jpg', tos_file_name: 'pixiv_img_v2/20261119/300_p0.jpg' }],
        });

        await runImport(src, dst);

        expect(dst.target.images.snapshot()).toEqual([workerDoc]);
        expect(doneEvent(dst, 'img_map')).toMatchObject({
            scanned: 1,
            inserted: 0,
            merged: 0,
            skipped: { pixiv_addr_exists: 1 },
        });
    });

    it('keeps only the first cloud image per pixiv_addr whatever the batch size', async () => {
        const imgMap = [
            { _id: oid(4), pixiv_addr: '400_p0.jpg', tos_file_name: 'a' },
            { _id: oid(5), pixiv_addr: '400_p0.jpg', tos_file_name: 'b' },
            { _id: oid(6), pixiv_addr: '401_p0.jpg', tos_file_name: 'c' },
            { _id: oid(7), pixiv_addr: '400_p0.jpg', tos_file_name: 'd' },
        ];
        const results: Document[][] = [];
        for (const batchSize of [1, 2, 100]) {
            const dst = cluster();
            await runImport(cloud({ imgMap }), dst, { batchSize });
            results.push(dst.target.images.snapshot());
            expect(doneEvent(dst, 'img_map')).toMatchObject({ inserted: 2, skipped: { pixiv_addr_exists: 2 } });
        }

        expect(results[0]).toEqual([imgMap[0]!, imgMap[2]!]);
        expect(results[1]).toEqual(results[0]!);
        expect(results[2]).toEqual(results[0]!);
    });
});

describe('runCloudImport: download_task and trans_map', () => {
    it('inserts download tasks missing from the cluster with their source _id and never overwrites', async () => {
        const created = new Date('2026-11-01T00:00:00.000Z');
        const clusterTask = {
            _id: oid(901),
            illust_id: '100',
            status: DownloadTaskStatus.Success,
            create_time: created,
            update_time: created,
            retry_time: 0,
        };
        const missingTask = {
            _id: oid(12),
            illust_id: '200',
            status: DownloadTaskStatus.Fail,
            create_time: created,
            update_time: created,
            retry_time: 2,
            last_run_error: 'timeout',
        };
        const dst = cluster({ downloadTasks: [clusterTask] });
        const src = cloud({
            downloadTask: [
                { _id: oid(11), illust_id: '100', status: DownloadTaskStatus.Pending, retry_time: 0 },
                missingTask,
                { _id: oid(13), status: DownloadTaskStatus.Pending },
            ],
        });

        await runImport(src, dst);

        expect(dst.target.downloadTasks.snapshot()).toEqual([clusterTask, missingTask]);
        expect(doneEvent(dst, 'download_task')).toMatchObject({
            target: 'pixiv_download_tasks',
            source_count: 3,
            scanned: 3,
            inserted: 1,
            merged: 0,
            skipped: { exists: 1, missing_illust_id: 1 },
        });
    });

    it('inserts translations missing from the cluster by origin and never overwrites', async () => {
        const clusterWord = { _id: oid(902), origin: '猫', translation: 'cat (edited)', has_translate: true };
        const missingWord = { _id: oid(22), origin: '犬', translation: 'dog', has_translate: true };
        const dst = cluster({ tagTranslations: [clusterWord] });
        const src = cloud({
            transMap: [
                { _id: oid(21), origin: '猫', translation: 'cat', has_translate: true },
                missingWord,
                { _id: oid(23), origin: '', has_translate: false },
            ],
        });

        await runImport(src, dst);

        expect(dst.target.tagTranslations.snapshot()).toEqual([clusterWord, missingWord]);
        expect(doneEvent(dst, 'trans_map')).toMatchObject({
            target: 'pixiv_tag_translations',
            inserted: 1,
            skipped: { exists: 1, missing_origin: 1 },
        });
    });
});

describe('runCloudImport: download_user_dict -> pixiv_author_downloads', () => {
    it('converts unix-second strings to numbers, counts invalid values and never overwrites', async () => {
        const dst = cluster({ authorDownloads: [{ _id: '14', last_download_at: 1800000000 }] });
        const src = cloud({
            authorDict: {
                '11': '1700000000',
                '12': 'abc',
                '13': '',
                '14': '1700000001',
                '15': '1.5e9',
                '16': '-5',
            },
        });

        await runImport(src, dst);

        expect(byId(dst.target.authorDownloads.snapshot())).toEqual([
            { _id: '11', last_download_at: 1700000000 },
            { _id: '14', last_download_at: 1800000000 },
        ]);
        expect(doneEvent(dst, 'download_user_dict')).toMatchObject({
            target: 'pixiv_author_downloads',
            source_count: 6,
            scanned: 6,
            inserted: 1,
            skipped: { exists: 1, invalid_value: 4 },
        });
    });
});

function fullCloud(): CloudData {
    return {
        imgMap: [
            { _id: oid(1), pixiv_addr: '1_p0.jpg', tos_file_name: 'k1', image_key: 'img1', width: 1, height: 1 },
            { _id: oid(2), pixiv_addr: '2_p0.jpg', tos_file_name: 'k2' },
            { _id: oid(3), pixiv_addr: '9_p0.jpg', tos_file_name: 'k3' },
            { _id: oid(4), pixiv_addr: '4_p0.jpg', tos_file_name: 'k4' },
            { _id: oid(5), pixiv_addr: '5_p0.jpg', tos_file_name: 'k5' },
        ],
        downloadTask: [
            { _id: oid(11), illust_id: '1', status: DownloadTaskStatus.Success, retry_time: 0 },
            { _id: oid(12), illust_id: '2', status: DownloadTaskStatus.Running, retry_time: 1 },
            { _id: oid(13), illust_id: '3', status: DownloadTaskStatus.Pending, retry_time: 0 },
        ],
        transMap: [
            { _id: oid(21), origin: 'a', translation: 'A', has_translate: true },
            { _id: oid(22), origin: 'b', has_translate: false },
        ],
        authorDict: { '1': '1700000000', '2': '1700000100', '3': 'oops' },
    };
}

function fullCluster(): ClusterData {
    return {
        images: [
            { _id: oid(1), pixiv_addr: '1_p0.jpg', tos_file_name: 'k1-old', image_key: 'img1-cluster', width: 800 },
            { _id: oid(900), pixiv_addr: '9_p0.jpg', tos_file_name: 'k9-worker' },
        ],
        downloadTasks: [{ _id: oid(910), illust_id: '3', status: DownloadTaskStatus.Success, retry_time: 0 }],
        tagTranslations: [{ _id: oid(920), origin: 'a', translation: 'A!', has_translate: true }],
        authorDownloads: [{ _id: '2', last_download_at: 1800000000 }],
    };
}

function snapshotTargets(dst: Cluster): Record<string, Document[]> {
    return {
        images: byId(dst.target.images.snapshot()),
        downloadTasks: byId(dst.target.downloadTasks.snapshot()),
        tagTranslations: byId(dst.target.tagTranslations.snapshot()),
        authorDownloads: byId(dst.target.authorDownloads.snapshot()),
    };
}

describe('runCloudImport: repeated runs', () => {
    it('a second run leaves every target collection unchanged and inserts nothing', async () => {
        const src = cloud(fullCloud());
        const dst = cluster(fullCluster());

        await runImport(src, dst, { runId: 'run-1' });
        const afterFirst = snapshotTargets(dst);
        await runImport(src, dst, { runId: 'run-2' });

        expect(snapshotTargets(dst)).toEqual(afterFirst);
        expect(doneEvent(dst, 'img_map', 'run-2')).toMatchObject({
            inserted: 0,
            merged: 4,
            skipped: { pixiv_addr_exists: 1 },
        });
        expect(doneEvent(dst, 'download_task', 'run-2')).toMatchObject({ inserted: 0, skipped: { exists: 3 } });
        expect(doneEvent(dst, 'trans_map', 'run-2')).toMatchObject({ inserted: 0, skipped: { exists: 2 } });
        expect(doneEvent(dst, 'download_user_dict', 'run-2')).toMatchObject({
            inserted: 0,
            skipped: { exists: 2, invalid_value: 1 },
        });
    });
});

describe('runCloudImport: batching', () => {
    it('pages every cloud collection by _id with the batch size as limit', async () => {
        const data = fullCloud();
        const src = cloud(data);
        const seen: Array<{ collection: string; filter: Document; options: Document }> = [];
        const spy = (col: ScanCollection): ScanCollection => ({
            collectionName: col.collectionName,
            countDocuments: (filter) => col.countDocuments(filter),
            find: (filter, options) => {
                seen.push({ collection: col.collectionName, filter, options });
                return col.find(filter, options);
            },
        });
        src.source.imgMap = spy(src.source.imgMap);
        src.source.downloadTask = spy(src.source.downloadTask);
        src.source.transMap = spy(src.source.transMap);
        const dst = cluster();

        await runImport(src, dst, { batchSize: 2 });

        expect(seen.length).toBeGreaterThanOrEqual(6);
        for (const call of seen) {
            expect(call.options).toEqual({ sort: { _id: 1 }, limit: 2 });
        }
        const imgMapFilters = seen.filter((c) => c.collection === 'img_map').map((c) => c.filter);
        expect(imgMapFilters.slice(0, 3)).toEqual([{}, { _id: { $gt: oid(2) } }, { _id: { $gt: oid(4) } }]);
        expect(doneEvent(dst, 'img_map')).toMatchObject({ source_count: 5, scanned: 5, inserted: 5, target_count: 5 });
    });

    it('fails the run when the _id cursor reaches fewer documents than the collection holds', async () => {
        // A string _id sorts before ObjectIds; $gt on a string never matches an ObjectId,
        // so a page that ends on the string stops the cursor early.
        const src = cloud({
            imgMap: [
                { _id: 'legacy-id', pixiv_addr: 'legacy_p0.jpg' },
                { _id: oid(1), pixiv_addr: '1_p0.jpg' },
                { _id: oid(2), pixiv_addr: '2_p0.jpg' },
            ],
        });
        const dst = cluster();

        const outcome = await runImport(src, dst, { batchSize: 1 });

        expect(outcome).toBe('failed');
        const failed = dst.runLog.snapshot().find((e) => e.event === 'failed');
        expect(failed?.error).toContain('img_map');
        expect(failed?.error).toContain('1 of 3');
        expect(dst.runLog.snapshot().some((e) => e.event === 'finished')).toBe(false);
    });
});

describe('runCloudImport: run log', () => {
    it('records started, one collection_done per collection with counts, then finished', async () => {
        const src = cloud(fullCloud());
        const dst = cluster(fullCluster());

        const outcome = await runImport(src, dst, { batchSize: 2 });

        expect(outcome).toBe('finished');
        const events = dst.runLog.snapshot();
        expect(events.map((e) => [e.seq, e.event, e.source ?? null])).toEqual([
            [0, 'started', null],
            [1, 'collection_done', 'img_map'],
            [2, 'collection_done', 'download_task'],
            [3, 'collection_done', 'trans_map'],
            [4, 'collection_done', 'download_user_dict'],
            [5, 'finished', null],
        ]);
        for (const event of events) {
            expect(event).toMatchObject({ run_id: 'run-1', at: NOW });
        }
        expect(events[1]).toMatchObject({
            target: 'pixiv_images',
            source_count: 5,
            scanned: 5,
            inserted: 3,
            merged: 1,
            skipped: { pixiv_addr_exists: 1 },
            target_count: 5,
        });
        for (const event of events.filter((e) => e.event === 'collection_done')) {
            const skipped = Object.values(event.skipped as Record<string, number>).reduce((a, b) => a + b, 0);
            expect(event.inserted + event.merged + skipped).toBe(event.scanned);
        }
        expect(src.state).toEqual({ opened: 1, closed: 1 });
    });

    it('only ever inserts into the run log; a later run leaves earlier events untouched', async () => {
        const src = cloud(fullCloud());
        const dst = cluster(fullCluster());

        await runImport(src, dst, { runId: 'run-1' });
        const firstRun = dst.runLog.snapshot();
        await runImport(src, dst, { runId: 'run-2' });

        expect(dst.runLog.snapshot().slice(0, firstRun.length)).toEqual(firstRun);
        expect(dst.runLog.snapshot()).toHaveLength(firstRun.length * 2);
        expect(new Set(dst.runLog.calls)).toEqual(new Set(['insertOne']));
    });

    it('records a failed event with the error and partial counts, and still closes the cloud connection', async () => {
        const src = cloud(fullCloud());
        src.source.transMap = {
            collectionName: 'trans_map',
            countDocuments: async () => 2,
            find: () => ({ toArray: async () => { throw new Error('connection reset by peer'); } }),
        };
        const dst = cluster();

        const outcome = await runImport(src, dst);

        expect(outcome).toBe('failed');
        const events = dst.runLog.snapshot();
        expect(events.map((e) => e.event)).toEqual(['started', 'collection_done', 'collection_done', 'failed']);
        expect(events[3]).toMatchObject({
            error: 'connection reset by peer',
            in_progress: { source: 'trans_map', target: 'pixiv_tag_translations', source_count: 2, scanned: 0 },
        });
        expect(src.state).toEqual({ opened: 1, closed: 1 });
    });

    it('records a failed event when the cloud connection cannot be opened', async () => {
        const dst = cluster();

        const outcome = await runImport(cloud(), dst, {
            openSource: async () => {
                throw new Error('connect ETIMEDOUT');
            },
        });

        expect(outcome).toBe('failed');
        expect(dst.runLog.snapshot().map((e) => [e.event, e.error ?? null])).toEqual([
            ['started', null],
            ['failed', 'connect ETIMEDOUT'],
        ]);
    });

    it('reports progress after every batch', async () => {
        const progress: Array<{ source: string; scanned: number }> = [];
        const dst = cluster();

        await runImport(cloud(fullCloud()), dst, {
            batchSize: 2,
            onProgress: (stats) => progress.push({ source: stats.source, scanned: stats.scanned }),
        });

        expect(progress.filter((p) => p.source === 'img_map').map((p) => p.scanned)).toEqual([2, 4, 5]);
    });
});

describe('listRecentRuns', () => {
    it('returns the most recent runs first, each with its events in order and without Mongo ids', async () => {
        const src = cloud(fullCloud());
        const dst = cluster(fullCluster());
        let tick = 0;
        const clock = () => new Date(NOW.getTime() + 1000 * tick++);

        for (const runId of ['run-1', 'run-2', 'run-3']) {
            await runImport(src, dst, { runId, now: clock });
        }

        const runs = await listRecentRuns(dst.runLog, 2);

        expect(runs.map((r) => r.run_id)).toEqual(['run-3', 'run-2']);
        expect(runs[0]!.events.map((e) => e.event)).toEqual([
            'started',
            'collection_done',
            'collection_done',
            'collection_done',
            'collection_done',
            'finished',
        ]);
        expect(runs[0]!.events.every((e) => !('_id' in e))).toBe(true);
    });
});
