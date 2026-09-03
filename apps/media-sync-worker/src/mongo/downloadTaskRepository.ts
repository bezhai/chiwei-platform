import type { MongoCollection } from '@inner/shared/mongo';
import { MongoServerError, type UpdateFilter } from 'mongodb';
import { DownloadTask, DownloadTaskStatus } from './types';

type DownloadTaskIndexCollection = Pick<
    MongoCollection<DownloadTask>,
    'createIndex'
>;

type DownloadTaskWriteCollection = Pick<
    MongoCollection<DownloadTask>,
    'findOneAndUpdate'
>;

export async function ensureDownloadTaskUniqueIndex(
    collection: DownloadTaskIndexCollection
): Promise<void> {
    await collection.createIndex(
        { illust_id: 1 },
        {
            unique: true,
            background: true,
            name: 'uniq_download_task_illust_id',
        }
    );
}

export async function insertDownloadTaskOnce(
    collection: DownloadTaskWriteCollection,
    illustId: string,
    now: Date = new Date()
): Promise<boolean> {
    const task = new DownloadTask({
        illust_id: illustId,
        status: DownloadTaskStatus.Pending,
        create_time: now,
        update_time: now,
        retry_time: 0,
    });

    try {
        const existing = await collection.findOneAndUpdate(
            { illust_id: illustId },
            { $setOnInsert: task } as UpdateFilter<DownloadTask>,
            { upsert: true, returnDocument: 'before' }
        );
        return existing === null;
    } catch (error) {
        if (
            error instanceof MongoServerError &&
            error.code === 11000 &&
            error.keyPattern?.illust_id === 1
        ) {
            return false;
        }
        throw error;
    }
}
