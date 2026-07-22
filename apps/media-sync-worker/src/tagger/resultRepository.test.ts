import { beforeEach, describe, expect, it } from 'bun:test';
import { TaggerResultRepository, type CollectionLike } from './resultRepository';
import type { TaggerImageResultDocument, TaggerTaskDocument } from './types';

class FakeCollection<T extends Record<string, unknown>> implements CollectionLike<T> {
    readonly indexes: Array<{ spec: Record<string, 1 | -1>; options?: Record<string, unknown> }> = [];
    readonly updates: Array<{ filter: Record<string, unknown>; update: Record<string, unknown>; options?: Record<string, unknown> }> = [];
    readonly bulkOperations: unknown[] = [];
    readonly finds: Array<{ filter: Record<string, unknown>; options?: Record<string, unknown> }> = [];
    findResult: T[] = [];

    async createIndex(spec: Record<string, 1 | -1>, options?: Record<string, unknown>): Promise<string> {
        this.indexes.push({ spec, options });
        return String(options?.name ?? Object.keys(spec).join('_'));
    }

    async updateOneRaw(
        filter: Record<string, unknown>,
        update: Record<string, unknown>,
        options?: Record<string, unknown>
    ): Promise<void> {
        this.updates.push({ filter, update, options });
    }

    async find(filter: Record<string, unknown>, options?: Record<string, unknown>): Promise<T[]> {
        this.finds.push({ filter, options });
        return this.findResult;
    }

    async findOneAndUpdate(
        filter: Record<string, unknown>,
        update: Record<string, unknown>,
        options?: Record<string, unknown>
    ): Promise<T | null> {
        this.updates.push({ filter, update, options });
        return this.findResult[0] ?? null;
    }

    async bulkWrite(operations: unknown[]): Promise<void> {
        this.bulkOperations.push(...operations);
    }
}

describe('TaggerResultRepository', () => {
    let tasks: FakeCollection<TaggerTaskDocument>;
    let imageResults: FakeCollection<TaggerImageResultDocument>;
    let repo: TaggerResultRepository;
    const now = new Date('2026-06-05T10:00:00.000Z');

    beforeEach(() => {
        tasks = new FakeCollection<TaggerTaskDocument>();
        imageResults = new FakeCollection<TaggerImageResultDocument>();
        repo = new TaggerResultRepository({ tasks, imageResults }, () => now);
    });

    it('creates task and image-result indexes', async () => {
        await repo.ensureIndexes();

        expect(tasks.indexes).toEqual([
            {
                spec: { task_id: 1 },
                options: { unique: true, background: true, name: 'idx_tagger_tasks_task_id_unique' },
            },
            {
                spec: { status: 1, next_reconcile_at: 1, submitted_at: 1 },
                options: { background: true, name: 'idx_tagger_tasks_reconcile_due' },
            },
            {
                spec: { status: 1, registering_at: 1 },
                options: { background: true, name: 'idx_tagger_tasks_registering_stale' },
            },
        ]);
        expect(imageResults.indexes).toEqual([
            {
                spec: { pixiv_addr: 1 },
                options: { unique: true, background: true, name: 'idx_tagger_image_results_pixiv_addr_unique' },
            },
            {
                spec: { task_id: 1 },
                options: { background: true, name: 'idx_tagger_image_results_task_id' },
            },
            {
                spec: { status: 1, next_attempt_at: 1, queued_at: 1 },
                options: { background: true, name: 'idx_tagger_image_results_outbox_due' },
            },
            {
                spec: { status: 1, processing_at: 1 },
                options: { background: true, name: 'idx_tagger_image_results_processing_stale' },
            },
            {
                spec: { projection_status: 1, next_projection_at: 1, completed_at: 1 },
                options: { background: true, name: 'idx_tagger_image_results_projection_due' },
            },
            {
                spec: { projection_status: 1, projection_processing_at: 1 },
                options: { background: true, name: 'idx_tagger_image_results_projection_stale' },
            },
        ]);
    });

    it('enqueues an image for asynchronous trigger processing', async () => {
        await repo.enqueueForTrigger('100363338_p1.jpg');

        expect(imageResults.updates).toEqual([
            {
                filter: { pixiv_addr: '100363338_p1.jpg' },
                update: {
                    $setOnInsert: {
                        pixiv_addr: '100363338_p1.jpg',
                        object_name: '100363338_p1.jpg',
                        generation: 1,
                        created_at: now,
                        status: 'queued',
                        queued_at: now,
                        next_attempt_at: now,
                        attempts: 0,
                        updated_at: now,
                        error: null,
                    },
                },
                options: { upsert: true },
            },
        ]);
    });

    it('uses insert-only enqueue fields so post-download replay preserves active or completed lifecycle', async () => {
        await repo.enqueueForTrigger('already-completed.jpg');

        const update = imageResults.updates[0].update;
        expect(update.$set).toBeUndefined();
        expect(update.$setOnInsert).toMatchObject({
            pixiv_addr: 'already-completed.jpg',
            generation: 1,
            status: 'queued',
            attempts: 0,
        });
    });

    it('claims only due trigger images for processing', async () => {
        imageResults.findResult = [
            {
                pixiv_addr: '100363338_p1.jpg',
                object_name: '100363338_p1.jpg',
                status: 'processing',
                created_at: now,
                updated_at: now,
            },
        ];

        const claimed = await repo.claimDueTriggerImage({
            path: '100363338_p1.jpg',
            leaseToken: 'trigger-owner-1',
            processingTimeoutMs: 600000,
        });

        expect(claimed?.pixiv_addr).toBe('100363338_p1.jpg');
        expect(imageResults.updates).toEqual([
            {
                filter: {
                    pixiv_addr: '100363338_p1.jpg',
                    $or: [
                        {
                            status: { $in: ['queued', 'retry'] },
                            $or: [
                                { next_attempt_at: { $lte: now } },
                                { next_attempt_at: { $exists: false } },
                            ],
                        },
                        {
                            status: 'processing',
                            processing_at: { $lte: new Date('2026-06-05T09:50:00.000Z') },
                        },
                    ],
                },
                update: {
                    $set: {
                        status: 'processing',
                        processing_at: now,
                        processing_lease_token: 'trigger-owner-1',
                        updated_at: now,
                        error: null,
                    },
                },
                options: { returnDocument: 'after' },
            },
        ]);
    });

    it('retries only while the same generation and processing lease still own the image', async () => {
        const nextAttemptAt = new Date('2026-06-05T10:01:00.000Z');

        await repo.markRetry({
            path: 'a.jpg',
            generation: 3,
            leaseToken: 'trigger-owner-3',
            attempts: 2,
            error: 'entry unavailable',
            nextAttemptAt,
        });

        expect(imageResults.updates[0]).toEqual({
            filter: {
                pixiv_addr: 'a.jpg',
                generation: 3,
                status: 'processing',
                processing_lease_token: 'trigger-owner-3',
            },
            update: {
                $set: {
                    status: 'retry',
                    attempts: 2,
                    next_attempt_at: nextAttemptAt,
                    updated_at: now,
                    error: 'entry unavailable',
                },
                $unset: {
                    processing_lease_token: '',
                    processing_at: '',
                },
            },
            options: undefined,
        });
    });

    it('marks a submitted task and each image as submitted with idempotent upserts', async () => {
        await repo.markSubmitted({
            taskId: 'task-1',
            claims: [
                { path: '100363338_p1.jpg', generation: 2, leaseToken: 'owner-a' },
                { path: '100285437_p0.png', generation: 3, leaseToken: 'owner-b' },
            ],
        });

        expect(tasks.updates).toEqual([
            {
                filter: { task_id: 'task-1' },
                update: {
                    $setOnInsert: {
                        task_id: 'task-1',
                        created_at: now,
                    },
                    $set: {
                        paths: ['100363338_p1.jpg', '100285437_p0.png'],
                        image_generations: {
                            '100363338_p1.jpg': 2,
                            '100285437_p0.png': 3,
                        },
                        image_processing_leases: {
                            '100363338_p1.jpg': 'owner-a',
                            '100285437_p0.png': 'owner-b',
                        },
                        status: 'registering',
                        registering_at: now,
                        updated_at: now,
                        error: null,
                    },
                },
                options: { upsert: true },
            },
            {
                filter: { task_id: 'task-1', status: 'registering' },
                update: {
                    $set: {
                        status: 'submitted',
                        submitted_at: now,
                        updated_at: now,
                        error: null,
                    },
                    $unset: {
                        image_processing_leases: '',
                        registering_at: '',
                    },
                },
                options: undefined,
            },
        ]);

        expect(imageResults.bulkOperations).toEqual([
            {
                updateOne: {
                    filter: {
                        pixiv_addr: '100363338_p1.jpg',
                        generation: 2,
                        status: 'processing',
                        processing_lease_token: 'owner-a',
                    },
                    update: {
                        $set: {
                            task_id: 'task-1',
                            status: 'submitted',
                            submitted_at: now,
                            updated_at: now,
                            error: null,
                        },
                        $unset: {
                            processing_lease_token: '',
                            processing_at: '',
                        },
                    },
                    upsert: false,
                },
            },
            {
                updateOne: {
                    filter: {
                        pixiv_addr: '100285437_p0.png',
                        generation: 3,
                        status: 'processing',
                        processing_lease_token: 'owner-b',
                    },
                    update: {
                        $set: {
                            task_id: 'task-1',
                            status: 'submitted',
                            submitted_at: now,
                            updated_at: now,
                            error: null,
                        },
                        $unset: {
                            processing_lease_token: '',
                            processing_at: '',
                        },
                    },
                    upsert: false,
                },
            },
        ]);
    });

    it('recovers a task registration interrupted between task and image writes', async () => {
        const registeringAt = new Date('2026-06-05T09:45:00.000Z');
        tasks.findResult = [{
            task_id: 'task-crash',
            paths: ['a.jpg'],
            image_generations: { 'a.jpg': 4 },
            image_processing_leases: { 'a.jpg': 'trigger-owner-4' },
            status: 'registering',
            registering_at: registeringAt,
            reconcile_lease_token: 'reconcile-owner',
            created_at: registeringAt,
            updated_at: registeringAt,
        }];

        await repo.finishRegisteringTask({
            taskId: 'task-crash',
            leaseToken: 'reconcile-owner',
        });

        expect(imageResults.bulkOperations).toEqual([{
            updateOne: {
                filter: {
                    pixiv_addr: 'a.jpg',
                    generation: 4,
                    status: 'processing',
                    processing_lease_token: 'trigger-owner-4',
                },
                update: {
                    $set: {
                        task_id: 'task-crash',
                        status: 'submitted',
                        submitted_at: registeringAt,
                        updated_at: now,
                        error: null,
                    },
                    $unset: {
                        processing_lease_token: '',
                        processing_at: '',
                    },
                },
                upsert: false,
            },
        }]);
        expect(tasks.updates[0]).toEqual({
            filter: {
                task_id: 'task-crash',
                status: 'registering',
                reconcile_lease_token: 'reconcile-owner',
            },
            update: {
                $set: {
                    status: 'submitted',
                    submitted_at: registeringAt,
                    updated_at: now,
                    error: null,
                },
                $unset: {
                    image_processing_leases: '',
                    registering_at: '',
                    reconcile_lease_token: '',
                    reconcile_lease_expires_at: '',
                    next_reconcile_at: '',
                },
            },
            options: undefined,
        });
    });

    it('stores callback rows as raw result payloads keyed by row id', async () => {
        const row = {
            id: '100363338_p1.jpg',
            schema_version: 1,
            wd14: { tags: [{ tag: 'solo', score: 0.9, category: 'general' }] },
        };

        tasks.findResult = [
            {
                task_id: 'task-1',
                paths: ['100363338_p1.jpg'],
                image_generations: { '100363338_p1.jpg': 4 },
                status: 'submitted',
                created_at: now,
                updated_at: now,
            },
        ];
        imageResults.findResult = [
            {
                pixiv_addr: '100363338_p1.jpg',
                object_name: '100363338_p1.jpg',
                task_id: 'task-1',
                generation: 4,
                status: 'submitted',
                created_at: now,
                updated_at: now,
            },
        ];

        await repo.applyCallback({
            task_id: 'task-1',
            status: 'completed',
            rows: [row],
            dups: [],
        });

        expect(tasks.updates).toEqual([
            {
                filter: { task_id: 'task-1', status: 'submitted' },
                update: {
                    $set: {
                        status: 'completed',
                        callback_payload: {
                            task_id: 'task-1',
                            status: 'completed',
                            rows: [row],
                            dups: [],
                        },
                        callback_at: now,
                        updated_at: now,
                        error: null,
                        stale_paths: [],
                    },
                    $unset: {
                        reconcile_lease_token: '',
                        reconcile_lease_expires_at: '',
                        next_reconcile_at: '',
                    },
                },
                options: undefined,
            },
        ]);

        expect(imageResults.updates).toEqual([
            {
                filter: {
                    pixiv_addr: '100363338_p1.jpg',
                    task_id: 'task-1',
                    generation: 4,
                },
                update: {
                    $set: {
                        status: 'completed',
                        result: row,
                        completed_at: now,
                        updated_at: now,
                        error: null,
                        projection_status: 'pending',
                        projection_attempts: 0,
                        next_projection_at: now,
                    },
                },
                options: { returnDocument: 'after' },
            },
        ]);
    });

    it('rejects partial callback rows before committing the task marker', async () => {
        tasks.findResult = [
            {
                task_id: 'task-1',
                paths: ['a.jpg', 'b.jpg'],
                image_generations: { 'a.jpg': 1, 'b.jpg': 1 },
                status: 'submitted',
                created_at: now,
                updated_at: now,
            },
        ];

        await expect(repo.applyCallback({
            task_id: 'task-1',
            status: 'completed',
            rows: [{ id: 'a.jpg', schema_version: 1 }],
        })).rejects.toThrow('missing rows for: b.jpg');

        expect(tasks.updates).toEqual([]);
        expect(imageResults.updates).toEqual([]);
    });

    it('marks image submit failures without requiring a tagger task id', async () => {
        await repo.markSubmitFailed({
            claims: [{
                path: '100363338_p1.jpg',
                generation: 4,
                leaseToken: 'owner-4',
            }],
            error: 'HTTP 503',
        });

        expect(imageResults.bulkOperations).toEqual([
            {
                updateOne: {
                    filter: {
                        pixiv_addr: '100363338_p1.jpg',
                        generation: 4,
                        status: 'processing',
                        processing_lease_token: 'owner-4',
                    },
                    update: {
                        $set: {
                            status: 'submit_failed',
                            error: 'HTTP 503',
                            updated_at: now,
                        },
                        $unset: {
                            processing_lease_token: '',
                            processing_at: '',
                        },
                    },
                    upsert: false,
                },
            },
        ]);
    });

    it('selects online projection work and gates legacy completed rows behind the backfill flag', async () => {
        await repo.findDueProjectionImages({
            limit: 5,
            processingTimeoutMs: 600_000,
            includeHistorical: false,
        });
        await repo.findDueProjectionImages({
            limit: 5,
            processingTimeoutMs: 600_000,
            includeHistorical: true,
        });

        expect((imageResults.finds[0].filter.$or as unknown[])).toHaveLength(2);
        expect((imageResults.finds[1].filter.$or as unknown[])).toHaveLength(3);
        expect((imageResults.finds[1].filter.$or as any[])[2]).toEqual({
            status: 'completed',
            result: { $exists: true },
            projection_status: { $exists: false },
        });
    });

    it('claims and completes a projection with task, generation and lease fencing', async () => {
        imageResults.findResult = [
            {
                pixiv_addr: 'a.jpg',
                object_name: 'a.jpg',
                task_id: 'task-1',
                generation: 2,
                status: 'completed',
                projection_status: 'processing',
                projection_lease_token: 'lease-1',
                created_at: now,
                updated_at: now,
            },
        ];

        await repo.claimProjection({
            path: 'a.jpg',
            taskId: 'task-1',
            generation: 2,
            leaseToken: 'lease-1',
            processingTimeoutMs: 600_000,
            includeHistorical: false,
        });
        await repo.markProjectionCompleted({
            path: 'a.jpg',
            taskId: 'task-1',
            generation: 2,
            leaseToken: 'lease-1',
            projectedAt: now,
        });

        expect(imageResults.updates[0].update).toMatchObject({
            $set: {
                projection_status: 'processing',
                projection_processing_at: now,
                projection_lease_token: 'lease-1',
            },
        });
        expect(imageResults.updates[1]).toEqual({
            filter: {
                pixiv_addr: 'a.jpg',
                task_id: 'task-1',
                generation: 2,
                projection_status: 'processing',
                projection_lease_token: 'lease-1',
            },
            update: {
                $set: {
                    projection_status: 'projected',
                    projected_at: now,
                    updated_at: now,
                    error: null,
                },
                $unset: {
                    projection_lease_token: '',
                    projection_processing_at: '',
                    next_projection_at: '',
                },
            },
            options: undefined,
        });
    });

    it('retries projection failures only while the same lease still owns the generation', async () => {
        const nextAttemptAt = new Date('2026-06-05T10:01:00.000Z');

        await repo.markProjectionRetry({
            path: 'a.jpg',
            taskId: 'task-1',
            generation: 2,
            leaseToken: 'lease-1',
            attempts: 3,
            error: 'local mongo unavailable',
            nextAttemptAt,
        });

        expect(imageResults.updates[0]).toEqual({
            filter: {
                pixiv_addr: 'a.jpg',
                task_id: 'task-1',
                generation: 2,
                projection_status: 'processing',
                projection_lease_token: 'lease-1',
            },
            update: {
                $set: {
                    projection_status: 'retry',
                    projection_attempts: 3,
                    next_projection_at: nextAttemptAt,
                    updated_at: now,
                    error: 'local mongo unavailable',
                },
                $unset: {
                    projection_lease_token: '',
                    projection_processing_at: '',
                },
            },
            options: undefined,
        });
    });

    it('claims only stale submitted tasks whose reconciliation lease is available', async () => {
        tasks.findResult = [
            {
                ...({} as TaggerTaskDocument),
                task_id: 'task-1',
                paths: ['a.jpg'],
                status: 'submitted',
                reconcile_lease_token: 'lease-1',
                created_at: now,
                updated_at: now,
            },
        ];

        await repo.findDueSubmittedTasks({ limit: 4, staleAfterMs: 600_000 });
        await repo.claimSubmittedTask({
            taskId: 'task-1',
            leaseToken: 'lease-1',
            staleAfterMs: 600_000,
            leaseExpiresAt: new Date('2026-06-05T10:01:00.000Z'),
        });

        const dueStatuses = (tasks.finds[0].filter.$and as any[])[0].$or;
        expect(dueStatuses[0]).toEqual({ status: 'requeueing' });
        expect(dueStatuses[1]).toMatchObject({ status: 'registering' });
        expect(dueStatuses[2]).toMatchObject({ status: 'submitted' });
        expect(tasks.updates[0].update).toMatchObject({
            $set: {
                reconcile_lease_token: 'lease-1',
                reconcile_lease_expires_at: new Date('2026-06-05T10:01:00.000Z'),
            },
        });
    });

    it('defers reconciliation only for the current task lease', async () => {
        const nextAttemptAt = new Date('2026-06-05T10:02:00.000Z');

        await repo.deferSubmittedTask({
            taskId: 'task-1',
            leaseToken: 'lease-1',
            nextAttemptAt,
            error: 'entry unavailable',
        });

        expect(tasks.updates[0]).toEqual({
            filter: { task_id: 'task-1', status: 'submitted', reconcile_lease_token: 'lease-1' },
            update: {
                $set: {
                    next_reconcile_at: nextAttemptAt,
                    reconcile_error: 'entry unavailable',
                    updated_at: now,
                },
                $unset: {
                    reconcile_lease_token: '',
                    reconcile_lease_expires_at: '',
                },
            },
            options: undefined,
        });
    });

    it('fences the old task before incrementing image generations for resubmission', async () => {
        tasks.findResult = [
            {
                task_id: 'task-1',
                paths: ['a.jpg'],
                image_generations: { 'a.jpg': 2 },
                status: 'requeueing',
                reconcile_lease_token: 'lease-1',
                created_at: now,
                updated_at: now,
            },
        ];
        const nextAttemptAt = new Date('2026-06-05T10:02:00.000Z');

        await repo.requeueSubmittedTask({
            taskId: 'task-1',
            leaseToken: 'lease-1',
            error: 'remote task missing',
            nextAttemptAt,
        });

        expect(tasks.updates[0]).toMatchObject({
            filter: {
                task_id: 'task-1',
                status: { $in: ['submitted', 'requeueing'] },
                reconcile_lease_token: 'lease-1',
            },
            update: { $set: { status: 'requeueing' } },
        });
        expect(imageResults.bulkOperations).toEqual([{
            updateOne: {
                filter: { pixiv_addr: 'a.jpg', task_id: 'task-1', generation: 2 },
                update: {
                    $set: {
                        generation: 3,
                        status: 'retry',
                        attempts: 0,
                        next_attempt_at: nextAttemptAt,
                        updated_at: now,
                        error: 'remote task missing',
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
        }]);
        expect(tasks.updates[1]).toMatchObject({
            filter: { task_id: 'task-1', status: 'requeueing', reconcile_lease_token: 'lease-1' },
            update: { $set: { status: 'failed' } },
        });
    });
});
