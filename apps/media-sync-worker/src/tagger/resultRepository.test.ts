import { beforeEach, describe, expect, it } from 'bun:test';
import { TaggerResultRepository, type CollectionLike } from './resultRepository';
import type { TaggerImageResultDocument, TaggerTaskDocument } from './types';

class FakeCollection<T extends Record<string, unknown>> implements CollectionLike<T> {
    readonly indexes: Array<{ spec: Record<string, 1 | -1>; options?: Record<string, unknown> }> = [];
    readonly updates: Array<{ filter: Record<string, unknown>; update: Record<string, unknown>; options?: Record<string, unknown> }> = [];
    readonly bulkOperations: unknown[] = [];

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
        ]);
    });

    it('marks a submitted task and each image as submitted with idempotent upserts', async () => {
        await repo.markSubmitted({
            taskId: 'task-1',
            paths: ['100363338_p1.jpg', '100285437_p0.png'],
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
                        status: 'submitted',
                        submitted_at: now,
                        updated_at: now,
                        error: null,
                    },
                },
                options: { upsert: true },
            },
        ]);

        expect(imageResults.bulkOperations).toEqual([
            {
                updateOne: {
                    filter: { pixiv_addr: '100363338_p1.jpg' },
                    update: {
                        $setOnInsert: {
                            pixiv_addr: '100363338_p1.jpg',
                            object_name: '100363338_p1.jpg',
                            created_at: now,
                        },
                        $set: {
                            task_id: 'task-1',
                            status: 'submitted',
                            submitted_at: now,
                            updated_at: now,
                            error: null,
                        },
                    },
                    upsert: true,
                },
            },
            {
                updateOne: {
                    filter: { pixiv_addr: '100285437_p0.png' },
                    update: {
                        $setOnInsert: {
                            pixiv_addr: '100285437_p0.png',
                            object_name: '100285437_p0.png',
                            created_at: now,
                        },
                        $set: {
                            task_id: 'task-1',
                            status: 'submitted',
                            submitted_at: now,
                            updated_at: now,
                            error: null,
                        },
                    },
                    upsert: true,
                },
            },
        ]);
    });

    it('stores callback rows as raw result payloads keyed by row id', async () => {
        const row = {
            id: '100363338_p1.jpg',
            schema_version: 1,
            wd14: { tags: [{ name: 'solo', score: 0.9 }] },
        };

        await repo.applyCallback({
            task_id: 'task-1',
            status: 'completed',
            rows: [row],
            dups: [],
        });

        expect(tasks.updates).toEqual([
            {
                filter: { task_id: 'task-1' },
                update: {
                    $setOnInsert: {
                        task_id: 'task-1',
                        paths: ['100363338_p1.jpg'],
                        created_at: now,
                    },
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
                    },
                },
                options: { upsert: true },
            },
        ]);

        expect(imageResults.bulkOperations).toEqual([
            {
                updateOne: {
                    filter: { pixiv_addr: '100363338_p1.jpg' },
                    update: {
                        $setOnInsert: {
                            pixiv_addr: '100363338_p1.jpg',
                            object_name: '100363338_p1.jpg',
                            created_at: now,
                        },
                        $set: {
                            task_id: 'task-1',
                            status: 'completed',
                            result: row,
                            completed_at: now,
                            updated_at: now,
                            error: null,
                        },
                    },
                    upsert: true,
                },
            },
        ]);
    });

    it('marks image submit failures without requiring a tagger task id', async () => {
        await repo.markSubmitFailed({
            paths: ['100363338_p1.jpg'],
            error: 'HTTP 503',
        });

        expect(imageResults.bulkOperations).toEqual([
            {
                updateOne: {
                    filter: { pixiv_addr: '100363338_p1.jpg' },
                    update: {
                        $setOnInsert: {
                            pixiv_addr: '100363338_p1.jpg',
                            object_name: '100363338_p1.jpg',
                            created_at: now,
                        },
                        $set: {
                            status: 'submit_failed',
                            error: 'HTTP 503',
                            updated_at: now,
                        },
                    },
                    upsert: true,
                },
            },
        ]);
    });
});
