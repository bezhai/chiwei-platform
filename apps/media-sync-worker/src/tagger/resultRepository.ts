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

export interface TaggerProcessingClaim {
    path: string;
    generation: number;
    leaseToken: string;
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
        await this.collections.tasks.createIndex(
            { status: 1, next_reconcile_at: 1, submitted_at: 1 },
            { background: true, name: 'idx_tagger_tasks_reconcile_due' }
        );
        await this.collections.tasks.createIndex(
            { status: 1, registering_at: 1 },
            { background: true, name: 'idx_tagger_tasks_registering_stale' }
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
        await this.collections.imageResults.createIndex(
            { projection_status: 1, next_projection_at: 1, completed_at: 1 },
            { background: true, name: 'idx_tagger_image_results_projection_due' }
        );
        await this.collections.imageResults.createIndex(
            { projection_status: 1, projection_processing_at: 1 },
            { background: true, name: 'idx_tagger_image_results_projection_stale' }
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
                    generation: 1,
                    created_at: at,
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

    async claimDueTriggerImage(params: {
        path: string;
        leaseToken: string;
        processingTimeoutMs: number;
    }): Promise<TaggerImageResultDocument | null> {
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
                    processing_lease_token: params.leaseToken,
                    updated_at: at,
                    error: null,
                },
            },
            { returnDocument: 'after' }
        );
    }

    async markRetry(params: TaggerProcessingClaim & {
        error: string;
        attempts: number;
        nextAttemptAt: Date;
    }): Promise<void> {
        const at = this.now();
        await this.collections.imageResults.updateOneRaw(
            processingOwnerFilter(params),
            {
                $set: {
                    status: 'retry',
                    attempts: params.attempts,
                    next_attempt_at: params.nextAttemptAt,
                    updated_at: at,
                    error: params.error,
                },
                $unset: {
                    processing_lease_token: '',
                    processing_at: '',
                },
            }
        );
    }

    async markSubmitted(params: { taskId: string; claims: TaggerProcessingClaim[] }): Promise<void> {
        const at = this.now();
        const imageGenerations = Object.fromEntries(
            params.claims.map((claim) => [claim.path, claim.generation])
        );
        const imageProcessingLeases = Object.fromEntries(
            params.claims.map((claim) => [claim.path, claim.leaseToken])
        );
        const paths = params.claims.map((claim) => claim.path);

        // Register the remote task before touching image ownership. Callback is
        // intentionally ignored while registering; if this process dies, the
        // reconcile worker can replay the conditional image transitions and
        // then publish the task as submitted.
        await this.collections.tasks.updateOneRaw(
            { task_id: params.taskId },
            {
                $setOnInsert: {
                    task_id: params.taskId,
                    created_at: at,
                },
                $set: {
                    paths,
                    image_generations: imageGenerations,
                    image_processing_leases: imageProcessingLeases,
                    status: 'registering',
                    registering_at: at,
                    updated_at: at,
                    error: null,
                },
            },
            { upsert: true }
        );

        const operations = params.claims.map((claim): AnyBulkWriteOperation<TaggerImageResultDocument> => ({
            updateOne: {
                filter: processingOwnerFilter(claim),
                update: {
                    $set: {
                        task_id: params.taskId,
                        status: 'submitted',
                        submitted_at: at,
                        updated_at: at,
                        error: null,
                    },
                    $unset: {
                        processing_lease_token: '',
                        processing_at: '',
                    },
                },
                upsert: false,
            },
        }));

        if (operations.length > 0) {
            await this.collections.imageResults.bulkWrite(operations, { ordered: false });
        }

        await this.collections.tasks.updateOneRaw(
            { task_id: params.taskId, status: 'registering' },
            {
                $set: {
                    status: 'submitted',
                    submitted_at: at,
                    updated_at: at,
                    error: null,
                },
                $unset: {
                    image_processing_leases: '',
                    registering_at: '',
                },
            }
        );
    }

    async finishRegisteringTask(params: {
        taskId: string;
        leaseToken: string;
    }): Promise<void> {
        const at = this.now();
        const [task] = await this.collections.tasks.find(
            {
                task_id: params.taskId,
                status: 'registering',
                reconcile_lease_token: params.leaseToken,
            },
            { limit: 1 }
        );
        if (!task) return;

        const generations = task.image_generations ?? {};
        const leases = task.image_processing_leases ?? {};
        const operations = task.paths.flatMap((path): AnyBulkWriteOperation<TaggerImageResultDocument>[] => {
            const generation = generations[path];
            const processingLease = leases[path];
            if (generation === undefined || !processingLease) return [];
            return [{
                updateOne: {
                    filter: processingOwnerFilter({ path, generation, leaseToken: processingLease }),
                    update: {
                        $set: {
                            task_id: params.taskId,
                            status: 'submitted',
                            submitted_at: task.registering_at ?? at,
                            updated_at: at,
                            error: null,
                        },
                        $unset: {
                            processing_lease_token: '',
                            processing_at: '',
                        },
                    },
                    upsert: false,
                },
            }];
        });
        if (operations.length > 0) {
            await this.collections.imageResults.bulkWrite(operations, { ordered: false });
        }

        await this.collections.tasks.updateOneRaw(
            {
                task_id: params.taskId,
                status: 'registering',
                reconcile_lease_token: params.leaseToken,
            },
            {
                $set: {
                    status: 'submitted',
                    submitted_at: task.registering_at ?? at,
                    updated_at: at,
                    error: null,
                },
                $unset: {
                    image_processing_leases: '',
                    registering_at: '',
                    reconcile_lease_token: '',
                    reconcile_lease_expires_at: '',
                    next_reconcile_at: '',
                },
            }
        );
    }

    async applyCallback(payload: TaggerCallbackPayload): Promise<void> {
        const at = this.now();
        const [task] = await this.collections.tasks.find(
            { task_id: payload.task_id },
            { limit: 1 }
        );
        if (!task) {
            throw new Error(`tagger callback task is not registered: ${payload.task_id}`);
        }
        if (task.status !== 'submitted') {
            return;
        }

        const rowsById = new Map(payload.rows.map((row) => [rowId(row), row]));
        const missingRows = task.paths.filter((path) => !rowsById.has(path));
        if (missingRows.length > 0) {
            throw new Error(`tagger callback missing rows for: ${missingRows.join(',')}`);
        }

        const stalePaths: string[] = [];
        for (const row of payload.rows) {
            const id = rowId(row);
            const generation = task.image_generations?.[id];
            if (generation === undefined) {
                stalePaths.push(id);
                continue;
            }
            const updated = await this.collections.imageResults.findOneAndUpdate(
                {
                    pixiv_addr: id,
                    task_id: payload.task_id,
                    generation,
                },
                {
                    $set: {
                        status: payload.status,
                        result: row,
                        completed_at: at,
                        updated_at: at,
                        error: null,
                        projection_status: 'pending',
                        projection_attempts: 0,
                        next_projection_at: at,
                    },
                },
                { returnDocument: 'after' }
            );
            if (!updated) {
                stalePaths.push(id);
            }
        }

        await this.collections.tasks.updateOneRaw(
            { task_id: payload.task_id, status: 'submitted' },
            {
                $set: {
                    status: payload.status,
                    callback_payload: payload,
                    callback_at: at,
                    updated_at: at,
                    error: null,
                    stale_paths: stalePaths,
                },
                $unset: {
                    reconcile_lease_token: '',
                    reconcile_lease_expires_at: '',
                    next_reconcile_at: '',
                },
            }
        );
    }

    async markSubmitFailed(params: { claims: TaggerProcessingClaim[]; error: string }): Promise<void> {
        const at = this.now();
        const operations = params.claims.map((claim): AnyBulkWriteOperation<TaggerImageResultDocument> => ({
            updateOne: {
                filter: processingOwnerFilter(claim),
                update: {
                    $set: {
                        status: 'submit_failed',
                        error: params.error,
                        updated_at: at,
                    },
                    $unset: {
                        processing_lease_token: '',
                        processing_at: '',
                    },
                },
                upsert: false,
            },
        }));

        if (operations.length > 0) {
            await this.collections.imageResults.bulkWrite(operations, { ordered: false });
        }
    }

    async findDueProjectionImages(params: {
        limit: number;
        processingTimeoutMs: number;
        includeHistorical: boolean;
    }): Promise<TaggerImageResultDocument[]> {
        const at = this.now();
        return this.collections.imageResults.find(
            { $or: projectionDueConditions(at, params.processingTimeoutMs, params.includeHistorical) },
            {
                sort: { next_projection_at: 1, completed_at: 1, created_at: 1 },
                limit: params.limit,
            }
        );
    }

    async claimProjection(params: {
        path: string;
        taskId: string;
        generation: number;
        leaseToken: string;
        processingTimeoutMs: number;
        includeHistorical: boolean;
    }): Promise<TaggerImageResultDocument | null> {
        const at = this.now();
        return this.collections.imageResults.findOneAndUpdate(
            {
                pixiv_addr: params.path,
                task_id: params.taskId,
                ...generationFilter(params.generation),
                $or: projectionDueConditions(at, params.processingTimeoutMs, params.includeHistorical),
            },
            {
                $set: {
                    generation: params.generation,
                    projection_status: 'processing',
                    projection_processing_at: at,
                    projection_lease_token: params.leaseToken,
                    updated_at: at,
                    error: null,
                },
            },
            { returnDocument: 'after' }
        );
    }

    async markProjectionCompleted(params: {
        path: string;
        taskId: string;
        generation: number;
        leaseToken: string;
        projectedAt: Date;
    }): Promise<void> {
        const at = this.now();
        await this.collections.imageResults.updateOneRaw(
            projectionOwnerFilter(params),
            {
                $set: {
                    projection_status: 'projected',
                    projected_at: params.projectedAt,
                    updated_at: at,
                    error: null,
                },
                $unset: {
                    projection_lease_token: '',
                    projection_processing_at: '',
                    next_projection_at: '',
                },
            }
        );
    }

    async markProjectionRetry(params: {
        path: string;
        taskId: string;
        generation: number;
        leaseToken: string;
        attempts: number;
        error: string;
        nextAttemptAt: Date;
    }): Promise<void> {
        const at = this.now();
        await this.collections.imageResults.updateOneRaw(
            projectionOwnerFilter(params),
            {
                $set: {
                    projection_status: 'retry',
                    projection_attempts: params.attempts,
                    next_projection_at: params.nextAttemptAt,
                    updated_at: at,
                    error: params.error,
                },
                $unset: {
                    projection_lease_token: '',
                    projection_processing_at: '',
                },
            }
        );
    }

    async findDueSubmittedTasks(params: {
        limit: number;
        staleAfterMs: number;
    }): Promise<TaggerTaskDocument[]> {
        const at = this.now();
        return this.collections.tasks.find(
            submittedTaskDueFilter(at, params.staleAfterMs),
            { sort: { next_reconcile_at: 1, submitted_at: 1 }, limit: params.limit }
        );
    }

    async claimSubmittedTask(params: {
        taskId: string;
        leaseToken: string;
        staleAfterMs: number;
        leaseExpiresAt: Date;
    }): Promise<TaggerTaskDocument | null> {
        const at = this.now();
        return this.collections.tasks.findOneAndUpdate(
            {
                task_id: params.taskId,
                ...submittedTaskDueFilter(at, params.staleAfterMs),
            },
            {
                $set: {
                    reconcile_lease_token: params.leaseToken,
                    reconcile_lease_expires_at: params.leaseExpiresAt,
                    updated_at: at,
                    reconcile_error: null,
                },
            },
            { returnDocument: 'after' }
        );
    }

    async deferSubmittedTask(params: {
        taskId: string;
        leaseToken: string;
        nextAttemptAt: Date;
        error: string | null;
    }): Promise<void> {
        const at = this.now();
        await this.collections.tasks.updateOneRaw(
            {
                task_id: params.taskId,
                status: 'submitted',
                reconcile_lease_token: params.leaseToken,
            },
            {
                $set: {
                    next_reconcile_at: params.nextAttemptAt,
                    reconcile_error: params.error,
                    updated_at: at,
                },
                $unset: {
                    reconcile_lease_token: '',
                    reconcile_lease_expires_at: '',
                },
            }
        );
    }

    async requeueSubmittedTask(params: {
        taskId: string;
        leaseToken: string;
        error: string;
        nextAttemptAt: Date;
    }): Promise<void> {
        const at = this.now();
        const task = await this.collections.tasks.findOneAndUpdate(
            {
                task_id: params.taskId,
                status: { $in: ['submitted', 'requeueing'] },
                reconcile_lease_token: params.leaseToken,
            },
            {
                $set: {
                    status: 'requeueing',
                    updated_at: at,
                    error: params.error,
                },
            },
            { returnDocument: 'after' }
        );
        if (!task) return;

        const imageGenerations = task.image_generations ?? {};
        const operations = task.paths.flatMap((path): AnyBulkWriteOperation<TaggerImageResultDocument>[] => {
            const generation = imageGenerations[path];
            if (generation === undefined) return [];
            return [{
                updateOne: {
                    filter: {
                        pixiv_addr: path,
                        task_id: params.taskId,
                        generation,
                    },
                    update: {
                        $set: {
                            generation: generation + 1,
                            status: 'retry',
                            attempts: 0,
                            next_attempt_at: params.nextAttemptAt,
                            updated_at: at,
                            error: params.error,
                        },
                        $unset: {
                            task_id: '',
                            submitted_at: '',
                            processing_at: '',
                            processing_lease_token: '',
                            result: '',
                            completed_at: '',
                            projection_status: '',
                            projection_attempts: '',
                            projection_processing_at: '',
                            projection_lease_token: '',
                            next_projection_at: '',
                            projected_at: '',
                        },
                    },
                    upsert: false,
                },
            }];
        });
        if (operations.length > 0) {
            await this.collections.imageResults.bulkWrite(operations, { ordered: false });
        }

        await this.collections.tasks.updateOneRaw(
            {
                task_id: params.taskId,
                status: 'requeueing',
                reconcile_lease_token: params.leaseToken,
            },
            {
                $set: {
                    status: 'failed',
                    updated_at: at,
                    error: params.error,
                },
                $unset: {
                    reconcile_lease_token: '',
                    reconcile_lease_expires_at: '',
                    next_reconcile_at: '',
                },
            }
        );
    }
}

function processingOwnerFilter(claim: TaggerProcessingClaim): Record<string, unknown> {
    return {
        pixiv_addr: claim.path,
        generation: claim.generation,
        status: 'processing',
        processing_lease_token: claim.leaseToken,
    };
}

function submittedTaskDueFilter(at: Date, staleAfterMs: number): Record<string, unknown> {
    return {
        $and: [
            {
                $or: [
                    { status: 'requeueing' },
                    {
                        status: 'registering',
                        registering_at: { $lte: new Date(at.getTime() - staleAfterMs) },
                    },
                    {
                        status: 'submitted',
                        submitted_at: { $lte: new Date(at.getTime() - staleAfterMs) },
                        $or: [
                            { next_reconcile_at: { $lte: at } },
                            { next_reconcile_at: { $exists: false } },
                        ],
                    },
                ],
            },
            {
                $or: [
                    { reconcile_lease_expires_at: { $lte: at } },
                    { reconcile_lease_expires_at: { $exists: false } },
                ],
            },
        ],
    };
}

function projectionDueConditions(
    at: Date,
    processingTimeoutMs: number,
    includeHistorical: boolean
): Record<string, unknown>[] {
    const conditions: Record<string, unknown>[] = [
        {
            projection_status: { $in: ['pending', 'retry'] },
            $or: [
                { next_projection_at: { $lte: at } },
                { next_projection_at: { $exists: false } },
            ],
        },
        {
            projection_status: 'processing',
            projection_processing_at: {
                $lte: new Date(at.getTime() - processingTimeoutMs),
            },
        },
    ];
    if (includeHistorical) {
        conditions.push({
            status: 'completed',
            result: { $exists: true },
            projection_status: { $exists: false },
        });
    }
    return conditions;
}

function generationFilter(generation: number): Record<string, unknown> {
    if (generation === 0) {
        return {
            $and: [
                {
                    $or: [
                        { generation: 0 },
                        { generation: { $exists: false } },
                    ],
                },
            ],
        };
    }
    return { generation };
}

function projectionOwnerFilter(params: {
    path: string;
    taskId: string;
    generation: number;
    leaseToken: string;
}): Record<string, unknown> {
    return {
        pixiv_addr: params.path,
        task_id: params.taskId,
        generation: params.generation,
        projection_status: 'processing',
        projection_lease_token: params.leaseToken,
    };
}

function rowId(row: Record<string, unknown>): string {
    if (typeof row.id !== 'string' || row.id === '') {
        throw new Error('tagger callback row.id must be a non-empty string');
    }
    return row.id;
}
