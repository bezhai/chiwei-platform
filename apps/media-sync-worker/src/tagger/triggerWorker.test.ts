import { describe, expect, it } from 'bun:test';
import { processTaggerTriggerBatch } from './triggerWorker';
import type { TaggerTriggerConfig } from './config';
import type { TaggerImageResultDocument } from './types';
import type { MinioSyncForTaggerResult } from '../storage/syncPage';

class FakeRepository {
    docs: TaggerImageResultDocument[] = [];
    claimed: string[] = [];
    claimResult: TaggerImageResultDocument | null | undefined;
    submitted: Array<{ taskId: string; paths: string[] }> = [];
    retries: Array<{ path: string; error: string; attempts: number; nextAttemptAt: Date }> = [];
    failed: Array<{ paths: string[]; error: string }> = [];

    async findDueTriggerImages() {
        return this.docs;
    }

    async claimDueTriggerImage(params: { path: string }) {
        this.claimed.push(params.path);
        if (this.claimResult !== undefined) {
            return this.claimResult;
        }
        return this.docs.find((doc) => doc.pixiv_addr === params.path) ?? null;
    }

    async markSubmitted(params: { taskId: string; paths: string[] }) {
        this.submitted.push(params);
    }

    async markRetry(params: { path: string; error: string; attempts: number; nextAttemptAt: Date }) {
        this.retries.push(params);
    }

    async markSubmitFailed(params: { paths: string[]; error: string }) {
        this.failed.push(params);
    }
}

class FakeSubmitClient {
    calls: Array<{ paths: string[]; callbackUrl: string }> = [];
    error: Error | null = null;

    async submit(req: { paths: string[]; callbackUrl: string }) {
        this.calls.push(req);
        if (this.error) {
            throw this.error;
        }
        return { taskId: 'task-1', status: 'accepted' };
    }
}

const config: TaggerTriggerConfig = {
    entryUrl: 'http://tagger-entry:8000',
    apiToken: 'token',
    callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
    batchSize: 2,
    submitTimeoutMs: 10000,
    submitRetries: 0,
    workerIdleDelayMs: 100,
    retryDelayMs: 60000,
    processingTimeoutMs: 600000,
    maxAttempts: 3,
};

function doc(pixivAddr: string, attempts = 0): TaggerImageResultDocument {
    return {
        pixiv_addr: pixivAddr,
        object_name: pixivAddr,
        status: 'queued',
        attempts,
        created_at: new Date('2026-06-05T10:00:00.000Z'),
        updated_at: new Date('2026-06-05T10:00:00.000Z'),
    };
}

describe('processTaggerTriggerBatch', () => {
    it('submits due queued images as one tagger batch from the background worker', async () => {
        const repository = new FakeRepository();
        const submitClient = new FakeSubmitClient();
        repository.docs = [doc('a.jpg'), doc('b.jpg')];

        const processed = await processTaggerTriggerBatch({
            repository: repository as any,
            submitClient: submitClient as any,
            config,
            syncPixivToMinio: async (pixivAddr): Promise<MinioSyncForTaggerResult> => ({
                status: 'synced',
                pixivAddr,
                ossKey: `pixiv_img_v2/20260605/${pixivAddr}`,
                objectName: pixivAddr,
            }),
        });

        expect(processed).toBe(2);
        expect(repository.claimed).toEqual(['a.jpg', 'b.jpg']);
        expect(submitClient.calls).toEqual([
            { paths: ['a.jpg', 'b.jpg'], callbackUrl: 'http://media-sync-worker/internal/tagger/callback' },
        ]);
        expect(repository.submitted).toEqual([{ taskId: 'task-1', paths: ['a.jpg', 'b.jpg'] }]);
        expect(repository.retries).toEqual([]);
    });

    it('retries MinIO timeouts instead of blocking download completion', async () => {
        const repository = new FakeRepository();
        const submitClient = new FakeSubmitClient();
        repository.docs = [doc('a.jpg', 1)];

        await processTaggerTriggerBatch({
            repository: repository as any,
            submitClient: submitClient as any,
            config,
            syncPixivToMinio: async (): Promise<MinioSyncForTaggerResult> => ({
                status: 'timeout',
                pixivAddr: 'a.jpg',
                ossKey: 'pixiv_img_v2/20260605/a.jpg',
                objectName: 'a.jpg',
                timeoutMs: 30000,
            }),
        });

        expect(submitClient.calls).toEqual([]);
        expect(repository.retries).toHaveLength(1);
        expect(repository.retries[0].path).toBe('a.jpg');
        expect(repository.retries[0].attempts).toBe(2);
        expect(repository.retries[0].error).toContain('reason=timeout');
    });

    it('submits synced images while retrying skipped images in the same batch', async () => {
        const repository = new FakeRepository();
        const submitClient = new FakeSubmitClient();
        repository.docs = [doc('a.jpg'), doc('b.jpg', 1)];

        await processTaggerTriggerBatch({
            repository: repository as any,
            submitClient: submitClient as any,
            config,
            syncPixivToMinio: async (pixivAddr): Promise<MinioSyncForTaggerResult> => {
                if (pixivAddr === 'b.jpg') {
                    return {
                        status: 'timeout',
                        pixivAddr,
                        ossKey: 'pixiv_img_v2/20260605/b.jpg',
                        objectName: 'b.jpg',
                        timeoutMs: 30000,
                    };
                }
                return {
                    status: 'synced',
                    pixivAddr,
                    ossKey: 'pixiv_img_v2/20260605/a.jpg',
                    objectName: 'a.jpg',
                };
            },
        });

        expect(submitClient.calls).toEqual([
            { paths: ['a.jpg'], callbackUrl: 'http://media-sync-worker/internal/tagger/callback' },
        ]);
        expect(repository.submitted).toEqual([{ taskId: 'task-1', paths: ['a.jpg'] }]);
        expect(repository.retries).toHaveLength(1);
        expect(repository.retries[0].path).toBe('b.jpg');
        expect(repository.retries[0].attempts).toBe(2);
    });

    it('does not submit when another worker has already claimed the image', async () => {
        const repository = new FakeRepository();
        const submitClient = new FakeSubmitClient();
        repository.docs = [doc('a.jpg')];
        repository.claimResult = null;

        const processed = await processTaggerTriggerBatch({
            repository: repository as any,
            submitClient: submitClient as any,
            config,
            syncPixivToMinio: async (): Promise<MinioSyncForTaggerResult> => ({
                status: 'synced',
                pixivAddr: 'a.jpg',
                ossKey: 'pixiv_img_v2/20260605/a.jpg',
                objectName: 'a.jpg',
            }),
        });

        expect(processed).toBe(1);
        expect(repository.claimed).toEqual(['a.jpg']);
        expect(submitClient.calls).toEqual([]);
        expect(repository.submitted).toEqual([]);
    });
});
