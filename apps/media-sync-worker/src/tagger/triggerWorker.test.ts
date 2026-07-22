import { describe, expect, it } from 'bun:test';
import { processTaggerTriggerBatch } from './triggerWorker';
import type { TaggerTriggerConfig } from './config';
import type { TaggerImageResultDocument } from './types';
import type { MinioSyncForTaggerResult } from '../storage/syncPage';
import { TaggerSubmitError } from './submitClient';

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

    async claimDueTriggerImage(params: { path: string; leaseToken: string }) {
        this.claimed.push(params.path);
        if (this.claimResult !== undefined) {
            return this.claimResult
                ? {
                    ...this.claimResult,
                    generation: this.claimResult.generation ?? 1,
                    status: 'processing',
                    processing_lease_token: params.leaseToken,
                }
                : null;
        }
        const found = this.docs.find((doc) => doc.pixiv_addr === params.path);
        return found
            ? {
                ...found,
                generation: found.generation ?? 1,
                status: 'processing',
                processing_lease_token: params.leaseToken,
            }
            : null;
    }

    async markSubmitted(params: {
        taskId: string;
        claims: Array<{ path: string; generation: number; leaseToken: string }>;
    }) {
        this.submitted.push({ taskId: params.taskId, paths: params.claims.map((claim) => claim.path) });
    }

    async markRetry(params: { path: string; error: string; attempts: number; nextAttemptAt: Date }) {
        this.retries.push(params);
    }

    async markSubmitFailed(params: {
        claims: Array<{ path: string; generation: number; leaseToken: string }>;
        error: string;
    }) {
        this.failed.push({ paths: params.claims.map((claim) => claim.path), error: params.error });
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
    reconcileAfterMs: 600000,
    reconcileLeaseMs: 60000,
    reconcileRetryDelayMs: 60000,
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

        expect(processed).toBe(0);
        expect(repository.claimed).toEqual(['a.jpg']);
        expect(submitClient.calls).toEqual([]);
        expect(repository.submitted).toEqual([]);
    });

    it('moves transient submit failures back to retry with the shared attempt budget', async () => {
        const repository = new FakeRepository();
        const submitClient = new FakeSubmitClient();
        repository.docs = [doc('a.jpg', 0), doc('b.jpg', 1)];
        submitClient.error = new Error('entry unavailable');

        await processTaggerTriggerBatch({
            repository: repository as any,
            submitClient: submitClient as any,
            config,
            syncPixivToMinio: async (pixivAddr): Promise<MinioSyncForTaggerResult> => ({
                status: 'synced',
                pixivAddr,
                ossKey: `pixiv/${pixivAddr}`,
                objectName: pixivAddr,
            }),
        });

        expect(repository.retries.map(({ path, attempts }) => ({ path, attempts }))).toEqual([
            { path: 'a.jpg', attempts: 1 },
            { path: 'b.jpg', attempts: 2 },
        ]);
        expect(repository.failed).toEqual([]);
    });

    it('makes a non-retryable 4xx submit failure terminal immediately', async () => {
        const repository = new FakeRepository();
        const submitClient = new FakeSubmitClient();
        repository.docs = [doc('a.jpg')];
        submitClient.error = new TaggerSubmitError('bad callback URL', 400);

        await processTaggerTriggerBatch({
            repository: repository as any,
            submitClient: submitClient as any,
            config,
            syncPixivToMinio: async (pixivAddr): Promise<MinioSyncForTaggerResult> => ({
                status: 'synced', pixivAddr, ossKey: pixivAddr, objectName: pixivAddr,
            }),
        });

        expect(repository.retries).toEqual([]);
        expect(repository.failed).toEqual([{ paths: ['a.jpg'], error: 'bad callback URL' }]);
    });

    it('makes a transient submit failure terminal only after max attempts', async () => {
        const repository = new FakeRepository();
        const submitClient = new FakeSubmitClient();
        repository.docs = [doc('a.jpg', 2)];
        submitClient.error = new Error('still unavailable');

        await processTaggerTriggerBatch({
            repository: repository as any,
            submitClient: submitClient as any,
            config,
            syncPixivToMinio: async (pixivAddr): Promise<MinioSyncForTaggerResult> => ({
                status: 'synced', pixivAddr, ossKey: pixivAddr, objectName: pixivAddr,
            }),
        });

        expect(repository.retries).toEqual([]);
        expect(repository.failed).toEqual([{ paths: ['a.jpg'], error: 'still unavailable' }]);
    });
});
