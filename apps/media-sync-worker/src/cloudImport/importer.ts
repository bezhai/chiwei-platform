/**
 * 一次性导入：把云机器上 worker 的状态合并进集群 Mongo 的 chiwei_pixiv。T6 随云机器一起删除。
 *
 * - img_map -> pixiv_images：按 _id 合并，沿用 buildPixivImageMirrorOperations 的语义
 *   （lark-service / tagger 写的字段只在插入时写）。集群里没有这个 _id、但已有同 pixiv_addr 的文档时跳过：
 *   切换后 worker 自己写的目录记录 _id 和 server 写进 img_map 的不同，重跑导入不能因此多出一份。
 * - download_task -> pixiv_download_tasks、trans_map -> pixiv_tag_translations：按 illust_id / origin，
 *   集群里没有的才插入（保留源 _id），已有的不动。
 * - Redis download_user_dict -> pixiv_author_downloads：值是 unix 秒的字符串，转成 number，集群里没有的才插入。
 *
 * 每次运行把事件追加写进运行记录（started / 每个集合一条 collection_done / finished 或 failed），不更新已有记录。
 * 重复运行的结果不变。
 */
import { BSON, type AnyBulkWriteOperation, type Document } from 'mongodb';
import { buildPixivImageMirrorOperations } from '../mongo/imageMirrorOperations';

/** 导入读取的云端集合；真实实现是 driver 的 Collection，测试用内存替身 */
export interface ScanCollection {
    readonly collectionName: string;
    find(filter: Document, options: { sort: Document; limit: number }): { toArray(): Promise<Document[]> };
    countDocuments(filter: Document): Promise<number>;
}

/** 导入写入的集群集合 */
export interface MergeCollection {
    readonly collectionName: string;
    find(filter: Document, options: { projection: Document }): { toArray(): Promise<Document[]> };
    countDocuments(filter: Document): Promise<number>;
    bulkWrite(
        operations: AnyBulkWriteOperation<Document>[],
        options: { ordered: boolean }
    ): Promise<{ matchedCount: number; upsertedCount: number }>;
}

export interface CloudSource {
    imgMap: ScanCollection;
    downloadTask: ScanCollection;
    transMap: ScanCollection;
    redis: { hgetall(key: string): Promise<Record<string, string>> };
}

export interface ClusterTarget {
    images: MergeCollection;
    downloadTasks: MergeCollection;
    tagTranslations: MergeCollection;
    authorDownloads: MergeCollection;
}

/** 运行记录只暴露追加和查询，从类型上就没有更新 */
export interface RunLog {
    insertOne(doc: Document): Promise<unknown>;
    find(filter: Document, options: { sort: Document; limit?: number }): { toArray(): Promise<Document[]> };
}

/** 一个集合的导入结果；inserted + merged + skipped 各项之和 = scanned */
export interface StepStats {
    source: string;
    target: string;
    source_count: number;
    scanned: number;
    inserted: number;
    merged: number;
    skipped: Record<string, number>;
}

export interface CloudImportParams {
    runId: string;
    openSource: () => Promise<{ source: CloudSource; close(): Promise<void> }>;
    target: ClusterTarget;
    runLog: RunLog;
    batchSize: number;
    now: () => Date;
    /** 每处理完一批调用一次，参数是副本 */
    onProgress?: (stats: StepStats) => void;
}

const AUTHOR_DOWNLOADS_KEY = 'download_user_dict';

interface Batching {
    size: number;
    onProgress?: (stats: StepStats) => void;
}

export async function runCloudImport(params: CloudImportParams): Promise<'finished' | 'failed'> {
    const { runId, target, batchSize } = params;
    const batching: Batching = { size: batchSize, onProgress: params.onProgress };
    let seq = 0;
    const record = (event: string, fields: Document) =>
        params.runLog.insertOne({ run_id: runId, seq: seq++, event, at: params.now(), ...fields });
    const progress: { current: StepStats | null } = { current: null };

    const runStep = async (
        sourceName: string,
        targetCollection: MergeCollection,
        body: (stats: StepStats) => Promise<void>
    ): Promise<void> => {
        const stats: StepStats = {
            source: sourceName,
            target: targetCollection.collectionName,
            source_count: 0,
            scanned: 0,
            inserted: 0,
            merged: 0,
            skipped: {},
        };
        progress.current = stats;
        await body(stats);
        const targetCount = await targetCollection.countDocuments({});
        await record('collection_done', { ...stats, target_count: targetCount });
        console.log(
            `Cloud import ${stats.source} -> ${stats.target}: run_id=${runId} source_count=${stats.source_count} scanned=${stats.scanned} inserted=${stats.inserted} merged=${stats.merged} skipped=${JSON.stringify(stats.skipped)} target_count=${targetCount}`
        );
        progress.current = null;
    };

    await record('started', { batch_size: batchSize });
    console.log(`Cloud import started: run_id=${runId} batch_size=${batchSize}`);

    let connection: { source: CloudSource; close(): Promise<void> } | null = null;
    try {
        connection = await params.openSource();
        const { source } = connection;
        await runStep(source.imgMap.collectionName, target.images, (stats) =>
            mergeImages(source.imgMap, target.images, stats, batching)
        );
        await runStep(source.downloadTask.collectionName, target.downloadTasks, (stats) =>
            insertMissingByKey(source.downloadTask, target.downloadTasks, 'illust_id', stats, batching)
        );
        await runStep(source.transMap.collectionName, target.tagTranslations, (stats) =>
            insertMissingByKey(source.transMap, target.tagTranslations, 'origin', stats, batching)
        );
        await runStep(AUTHOR_DOWNLOADS_KEY, target.authorDownloads, (stats) =>
            insertMissingAuthorDownloads(source.redis, target.authorDownloads, stats, batching)
        );
    } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        console.error(`Cloud import failed: run_id=${runId} step=${progress.current?.source ?? 'connect'} error=${error}`);
        await record('failed', { error, in_progress: progress.current ? copyStats(progress.current) : null });
        return 'failed';
    } finally {
        await connection?.close().catch((err) => {
            console.warn(`Cloud import: closing cloud connections failed: run_id=${runId} error=${String(err)}`);
        });
    }

    await record('finished', {});
    console.log(`Cloud import finished: run_id=${runId}`);
    return 'finished';
}

export interface RunSummary {
    run_id: string;
    events: Document[];
}

/** 最近 limit 次运行（按开始时间倒序），每次运行的事件按写入顺序 */
export async function listRecentRuns(runLog: RunLog, limit: number): Promise<RunSummary[]> {
    const starts = await runLog.find({ event: 'started' }, { sort: { at: -1 }, limit }).toArray();
    if (starts.length === 0) {
        return [];
    }
    const runIds: string[] = starts.map((start) => start.run_id);
    const events = await runLog.find({ run_id: { $in: runIds } }, { sort: { seq: 1 } }).toArray();
    const byRun = new Map<string, Document[]>(runIds.map((runId) => [runId, []]));
    for (const { _id, ...event } of events) {
        byRun.get(event.run_id)?.push(event);
    }
    return runIds.map((runId) => ({ run_id: runId, events: byRun.get(runId) ?? [] }));
}

async function mergeImages(
    source: ScanCollection,
    target: MergeCollection,
    stats: StepStats,
    batching: Batching
): Promise<void> {
    await scanInBatches(source, stats, batching, async (batch) => {
        const existing = await target
            .find({ _id: { $in: batch.map((doc) => doc._id) } }, { projection: { _id: 1 } })
            .toArray();
        const existingIds = new Set(existing.map((doc) => idKey(doc._id)));
        const isNew = (doc: Document) => !existingIds.has(idKey(doc._id));

        const newAddrs = [...new Set(batch.filter(isNew).map((doc) => doc.pixiv_addr).filter(isNonEmptyString))];
        const takenAddrs = new Set<string>();
        if (newAddrs.length > 0) {
            const holders = await target
                .find({ pixiv_addr: { $in: newAddrs } }, { projection: { pixiv_addr: 1 } })
                .toArray();
            for (const holder of holders) {
                takenAddrs.add(holder.pixiv_addr);
            }
        }

        const toWrite: Document[] = [];
        for (const doc of batch) {
            if (isNew(doc) && isNonEmptyString(doc.pixiv_addr)) {
                if (takenAddrs.has(doc.pixiv_addr)) {
                    countSkip(stats, 'pixiv_addr_exists');
                    continue;
                }
                // 同一批里后面同 pixiv_addr 的文档也要跳过，结果才和批大小无关
                takenAddrs.add(doc.pixiv_addr);
            }
            toWrite.push(doc);
        }
        if (toWrite.length > 0) {
            const result = await target.bulkWrite(buildPixivImageMirrorOperations(toWrite), { ordered: false });
            stats.inserted += result.upsertedCount;
            stats.merged += result.matchedCount;
        }
    });
}

async function insertMissingByKey(
    source: ScanCollection,
    target: MergeCollection,
    key: 'illust_id' | 'origin',
    stats: StepStats,
    batching: Batching
): Promise<void> {
    await scanInBatches(source, stats, batching, async (batch) => {
        const operations: AnyBulkWriteOperation<Document>[] = [];
        for (const doc of batch) {
            const value = doc[key];
            if (value === undefined || value === null || value === '') {
                countSkip(stats, `missing_${key}`);
                continue;
            }
            operations.push({
                updateOne: { filter: { [key]: value }, update: { $setOnInsert: doc }, upsert: true },
            });
        }
        await insertMissing(target, operations, stats);
    });
}

async function insertMissingAuthorDownloads(
    redis: CloudSource['redis'],
    target: MergeCollection,
    stats: StepStats,
    batching: Batching
): Promise<void> {
    // hash 的规模是关注的作者数，一次读完；写入仍按批
    const entries = Object.entries(await redis.hgetall(AUTHOR_DOWNLOADS_KEY));
    stats.source_count = entries.length;
    for (let start = 0; start < entries.length; start += batching.size) {
        const chunk = entries.slice(start, start + batching.size);
        const operations: AnyBulkWriteOperation<Document>[] = [];
        for (const [authorId, raw] of chunk) {
            const seconds = parseUnixSeconds(raw);
            if (seconds === null) {
                countSkip(stats, 'invalid_value');
                continue;
            }
            // pixiv_author_downloads 的 _id 是作者 ID 字符串；driver 的 Filter<Document> 默认把 _id 当 ObjectId
            const filter: Document = { _id: authorId };
            operations.push({
                updateOne: {
                    filter,
                    update: { $setOnInsert: { last_download_at: seconds } },
                    upsert: true,
                },
            });
        }
        await insertMissing(target, operations, stats);
        stats.scanned += chunk.length;
        batching.onProgress?.(copyStats(stats));
    }
}

/** upsert + $setOnInsert：已有文档只匹配不修改，计为 exists */
async function insertMissing(
    target: MergeCollection,
    operations: AnyBulkWriteOperation<Document>[],
    stats: StepStats
): Promise<void> {
    if (operations.length === 0) {
        return;
    }
    const result = await target.bulkWrite(operations, { ordered: false });
    stats.inserted += result.upsertedCount;
    countSkip(stats, 'exists', result.matchedCount);
}

/**
 * 按 _id 升序分页读完一个集合。
 *
 * $gt 只匹配同一 BSON 类型的 _id，集合里混有不同类型的 _id 时，游标会在类型边界提前结束。
 * 读完后和开始前的总数比对，少读了就让这次运行失败，不悄悄漏掉文档。
 */
async function scanInBatches(
    source: ScanCollection,
    stats: StepStats,
    batching: Batching,
    handle: (batch: Document[]) => Promise<void>
): Promise<void> {
    stats.source_count = await source.countDocuments({});
    let filter: Document = {};
    for (;;) {
        const batch = await source.find(filter, { sort: { _id: 1 }, limit: batching.size }).toArray();
        if (batch.length === 0) {
            break;
        }
        await handle(batch);
        stats.scanned += batch.length;
        batching.onProgress?.(copyStats(stats));
        if (batch.length < batching.size) {
            break;
        }
        filter = { _id: { $gt: batch[batch.length - 1]!._id } };
    }
    if (stats.scanned < stats.source_count) {
        throw new Error(
            `${source.collectionName}: the _id cursor reached ${stats.scanned} of ${stats.source_count} documents; ` +
            'either documents were deleted during the import or some _id values have another BSON type and cannot be paged with $gt'
        );
    }
}

function parseUnixSeconds(raw: string): number | null {
    if (!/^\d+$/.test(raw)) {
        return null;
    }
    const seconds = Number(raw);
    return Number.isSafeInteger(seconds) ? seconds : null;
}

function idKey(id: unknown): string {
    return BSON.EJSON.stringify({ id }, { relaxed: false });
}

function isNonEmptyString(value: unknown): value is string {
    return typeof value === 'string' && value !== '';
}

function countSkip(stats: StepStats, reason: string, count = 1): void {
    if (count > 0) {
        stats.skipped[reason] = (stats.skipped[reason] ?? 0) + count;
    }
}

function copyStats(stats: StepStats): StepStats {
    return { ...stats, skipped: { ...stats.skipped } };
}
