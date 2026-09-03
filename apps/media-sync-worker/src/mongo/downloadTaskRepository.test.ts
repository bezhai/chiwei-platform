import { describe, expect, it, mock } from 'bun:test';
import { MongoServerError } from 'mongodb';
import {
    ensureDownloadTaskUniqueIndex,
    insertDownloadTaskOnce,
} from './downloadTaskRepository';
import { DownloadTaskStatus } from './types';

describe('ensureDownloadTaskUniqueIndex', () => {
    it('declares illust_id as a unique repository invariant', async () => {
        const createIndex = mock(async () => 'uniq_download_task_illust_id');

        await ensureDownloadTaskUniqueIndex({ createIndex } as any);

        expect(createIndex).toHaveBeenCalledWith(
            { illust_id: 1 },
            {
                unique: true,
                background: true,
                name: 'uniq_download_task_illust_id',
            }
        );
    });
});

describe('insertDownloadTaskOnce', () => {
    const now = new Date('2026-07-24T12:00:00.000Z');

    it('uses one atomic upsert and reports a new task', async () => {
        const findOneAndUpdate = mock(async () => null);

        const inserted = await insertDownloadTaskOnce(
            { findOneAndUpdate } as any,
            '123',
            now
        );

        expect(inserted).toBe(true);
        expect(findOneAndUpdate).toHaveBeenCalledWith(
            { illust_id: '123' },
            {
                $setOnInsert: {
                    illust_id: '123',
                    status: DownloadTaskStatus.Pending,
                    create_time: now,
                    update_time: now,
                    retry_time: 0,
                    last_run_time: undefined,
                    last_run_error: undefined,
                },
            },
            { upsert: true, returnDocument: 'before' }
        );
    });

    it('reports an existing task without replacing its state', async () => {
        const findOneAndUpdate = mock(async () => ({
            illust_id: '123',
            status: DownloadTaskStatus.Success,
        }));

        expect(
            await insertDownloadTaskOnce({ findOneAndUpdate } as any, '123', now)
        ).toBe(false);
    });

    it('treats a duplicate-key race as an existing task', async () => {
        const findOneAndUpdate = mock(async () => {
            throw new MongoServerError({
                message: 'duplicate key',
                code: 11000,
                keyPattern: { illust_id: 1 },
            });
        });

        expect(
            await insertDownloadTaskOnce({ findOneAndUpdate } as any, '123', now)
        ).toBe(false);
    });

    it('does not hide a duplicate-key error for an unrelated constraint', async () => {
        const failure = new MongoServerError({
            message: 'duplicate key',
            code: 11000,
            keyPattern: { other_key: 1 },
        });
        const findOneAndUpdate = mock(async () => {
            throw failure;
        });

        const promise = insertDownloadTaskOnce(
            { findOneAndUpdate } as any,
            '123',
            now
        );

        expect(promise).rejects.toBe(failure);
        await promise.catch(() => {});
    });

    it('preserves non-duplicate database errors', async () => {
        const failure = new Error('mongo unavailable');
        const findOneAndUpdate = mock(async () => {
            throw failure;
        });

        const promise = insertDownloadTaskOnce(
            { findOneAndUpdate } as any,
            '123',
            now
        );

        expect(promise).rejects.toBe(failure);
        await promise.catch(() => {});
    });
});
