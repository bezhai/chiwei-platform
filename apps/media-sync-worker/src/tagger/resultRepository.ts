import type {
    AnyBulkWriteOperation,
    CreateIndexesOptions,
    FindOptions,
    IndexSpecification,
    UpdateFilter,
    UpdateOptions,
} from 'mongodb';
import type { TaggerCallbackPayload, TaggerImageResultDocument, TaggerTaskDocument } from './types';

export interface CollectionLike<T extends Record<string, unknown>> {
    createIndex(indexSpec: IndexSpecification, options?: CreateIndexesOptions): Promise<string>;
    find(filter: Record<string, unknown>, options?: FindOptions<T>): Promise<T[]>;
    findOneAndUpdate(
        filter: Record<string, unknown>,
        update: UpdateFilter<T>,
        options?: { upsert?: boolean; returnDocument?: 'before' | 'after' }
    ): Promise<T | null>;
    updateOneRaw(filter: Record<string, unknown>, update: UpdateFilter<T>, options?: UpdateOptions): Promise<void>;
    bulkWrite(operations: AnyBulkWriteOperation<T>[], options?: { ordered?: boolean }): Promise<unknown>;
}

export interface TaggerResultCollections {
    tasks: CollectionLike<TaggerTaskDocument>;
    imageResults: CollectionLike<TaggerImageResultDocument>;
}

export class TaggerResultRepository {
    constructor(
        private readonly collections: TaggerResultCollections,
        private readonly now: () => Date = () => new Date()
    ) {}

    async ensureIndexes(): Promise<void> {
        await this.collections.tasks.createIndex(
            { task_id: 1 },
            { unique: true, background: true, name: 'idx_tagger_tasks_task_id_unique' }
        );
        await this.collections.imageResults.createIndex(
            { pixiv_addr: 1 },
            { unique: true, background: true, name: 'idx_tagger_image_results_pixiv_addr_unique' }
        );
        await this.collections.imageResults.createIndex(
            { task_id: 1 },
            { background: true, name: 'idx_tagger_image_results_task_id' }
        );
        await this.collections.imageResults.createIndex(
            { status: 1, next_attempt_at: 1, queued_at: 1 },
            { background: true, name: 'idx_tagger_image_results_outbox_due' }
        );
        await this.collections.imageResults.createIndex(
            { status: 1, processing_at: 1 },
            { background: true, name: 'idx_tagger_image_results_processing_stale' }
        );
    }

    async enqueueForTrigger(pixivAddr: string): Promise<void> {
        const at = this.now();
        await this.collections.imageResults.updateOneRaw(
            { pixiv_addr: pixivAddr },
            {
                $setOnInsert: {
                    pixiv_addr: pixivAddr,
                    object_name: pixivAddr,
                    created_at: at,
                },
                $set: {
                    status: 'queued',
                    queued_at: at,
                    next_attempt_at: at,
                    attempts: 0,
                    updated_at: at,
                    error: null,
                },
            },
            { upsert: true }
        );
    }

    async findDueTriggerImages(params: { limit: number; processingTimeoutMs: number }): Promise<TaggerImageResultDocument[]> {
        const at = this.now();
        const processingStaleBefore = new Date(at.getTime() - params.processingTimeoutMs);
        return this.collections.imageResults.find(
            {
                $or: [
                    {
                        status: { $in: ['queued', 'retry'] },
                        $or: [
                            { next_attempt_at: { $lte: at } },
                            { next_attempt_at: { $exists: false } },
                        ],
                    },
                    {
                        status: 'processing',
                        processing_at: { $lte: processingStaleBefore },
                    },
                ],
            },
            {
                sort: { next_attempt_at: 1, queued_at: 1, created_at: 1 },
                limit: params.limit,
            }
        );
    }

    async claimDueTriggerImage(params: { path: string; processingTimeoutMs: number }): Promise<TaggerImageResultDocument | null> {
        const at = this.now();
        const processingStaleBefore = new Date(at.getTime() - params.processingTimeoutMs);
        return this.collections.imageResults.findOneAndUpdate(
            {
                pixiv_addr: params.path,
                $or: [
                    {
                        status: { $in: ['queued', 'retry'] },
                        $or: [
                            { next_attempt_at: { $lte: at } },
                            { next_attempt_at: { $exists: false } },
                        ],
                    },
                    {
                        status: 'processing',
                        processing_at: { $lte: processingStaleBefore },
                    },
                ],
            },
            {
                $set: {
                    status: 'processing',
                    processing_at: at,
                    updated_at: at,
                    error: null,
                },
            },
            { returnDocument: 'after' }
        );
    }

    async markRetry(params: { path: string; error: string; attempts: number; nextAttemptAt: Date }): Promise<void> {
        const at = this.now();
        await this.collections.imageResults.updateOneRaw(
            { pixiv_addr: params.path },
            {
                $set: {
                    status: 'retry',
                    attempts: params.attempts,
                    next_attempt_at: params.nextAttemptAt,
                    updated_at: at,
                    error: params.error,
                },
            }
        );
    }

    async markSubmitted(params: { taskId: string; paths: string[] }): Promise<void> {
        const at = this.now();
        await this.collections.tasks.updateOneRaw(
            { task_id: params.taskId },
            {
                $setOnInsert: {
                    task_id: params.taskId,
                    created_at: at,
                },
                $set: {
                    paths: params.paths,
                    status: 'submitted',
                    submitted_at: at,
                    updated_at: at,
                    error: null,
                },
            },
            { upsert: true }
        );

        const operations = params.paths.map((path): AnyBulkWriteOperation<TaggerImageResultDocument> => ({
            updateOne: {
                filter: { pixiv_addr: path },
                update: {
                    $setOnInsert: {
                        pixiv_addr: path,
                        object_name: path,
                        created_at: at,
                    },
                    $set: {
                        task_id: params.taskId,
                        status: 'submitted',
                        submitted_at: at,
                        updated_at: at,
                        error: null,
                    },
                },
                upsert: true,
            },
        }));

        if (operations.length > 0) {
            await this.collections.imageResults.bulkWrite(operations, { ordered: false });
        }
    }

    async applyCallback(payload: TaggerCallbackPayload): Promise<void> {
        const at = this.now();
        const paths = payload.rows.map((row) => rowId(row));

        await this.collections.tasks.updateOneRaw(
            { task_id: payload.task_id },
            {
                $setOnInsert: {
                    task_id: payload.task_id,
                    paths,
                    created_at: at,
                },
                $set: {
                    status: payload.status,
                    callback_payload: payload,
                    callback_at: at,
                    updated_at: at,
                    error: null,
                },
            },
            { upsert: true }
        );

        const operations = payload.rows.map((row): AnyBulkWriteOperation<TaggerImageResultDocument> => {
            const id = rowId(row);
            return {
                updateOne: {
                    filter: { pixiv_addr: id },
                    update: {
                        $setOnInsert: {
                            pixiv_addr: id,
                            object_name: id,
                            created_at: at,
                        },
                        $set: {
                            task_id: payload.task_id,
                            status: payload.status,
                            result: row,
                            completed_at: at,
                            updated_at: at,
                            error: null,
                        },
                    },
                    upsert: true,
                },
            };
        });

        if (operations.length > 0) {
            await this.collections.imageResults.bulkWrite(operations, { ordered: false });
        }
    }

    async markSubmitFailed(params: { paths: string[]; error: string }): Promise<void> {
        const at = this.now();
        const operations = params.paths.map((path): AnyBulkWriteOperation<TaggerImageResultDocument> => ({
            updateOne: {
                filter: { pixiv_addr: path },
                update: {
                    $setOnInsert: {
                        pixiv_addr: path,
                        object_name: path,
                        created_at: at,
                    },
                    $set: {
                        status: 'submit_failed',
                        error: params.error,
                        updated_at: at,
                    },
                },
                upsert: true,
            },
        }));

        if (operations.length > 0) {
            await this.collections.imageResults.bulkWrite(operations, { ordered: false });
        }
    }
}

function rowId(row: Record<string, unknown>): string {
    if (typeof row.id !== 'string' || row.id === '') {
        throw new Error('tagger callback row.id must be a non-empty string');
    }
    return row.id;
}
