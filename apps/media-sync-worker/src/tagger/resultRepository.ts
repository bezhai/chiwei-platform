import type {
    AnyBulkWriteOperation,
    CreateIndexesOptions,
    IndexSpecification,
    UpdateFilter,
    UpdateOptions,
} from 'mongodb';
import type { TaggerCallbackPayload, TaggerImageResultDocument, TaggerTaskDocument } from './types';

export interface CollectionLike<T extends Record<string, unknown>> {
    createIndex(indexSpec: IndexSpecification, options?: CreateIndexesOptions): Promise<string>;
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
