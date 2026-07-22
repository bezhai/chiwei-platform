import { describe, expect, it } from 'bun:test';
import { triggerTaggerForPixivAddr, triggerTaggerForPixivAddrs, type TriggerTaggerDeps } from './trigger';
import type { MinioSyncForTaggerResult } from '../storage/syncPage';
import { TaggerSubmitError, type TaggerSubmitResult } from './submitClient';

class FakeSubmitClient {
    calls: Array<{ paths: string[]; callbackUrl: string }> = [];
    nextResult: TaggerSubmitResult = { taskId: 'task-1', status: 'accepted' };
    nextError: Error | null = null;

    async submit(req: { paths: string[]; callbackUrl: string }) {
        this.calls.push(req);
        if (this.nextError) {
            throw this.nextError;
        }
        return this.nextResult;
    }
}

function deps(syncResult: MinioSyncForTaggerResult): TriggerTaggerDeps {
    const submitClient = new FakeSubmitClient();
    return {
        syncPixivToMinio: async () => syncResult,
        submitClient,
        callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
    };
}

describe('triggerTaggerForPixivAddr', () => {
    it('skips submit when MinIO is not synced', async () => {
        const d = deps({ status: 'missing_key', pixivAddr: 'a.jpg' });

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toEqual({ status: 'skipped', reason: 'missing_key' });
        expect((d.submitClient as FakeSubmitClient).calls).toEqual([]);
    });

    it('keeps MinIO timeout details when submit is skipped', async () => {
        const d = deps({
            status: 'timeout',
            pixivAddr: 'a.jpg',
            ossKey: 'pixiv_img_v2/20260605/a.jpg',
            objectName: 'a.jpg',
            timeoutMs: 30000,
        });

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toEqual({
            status: 'skipped',
            reason: 'timeout',
            ossKey: 'pixiv_img_v2/20260605/a.jpg',
            objectName: 'a.jpg',
            timeoutMs: 30000,
        });
        expect((d.submitClient as FakeSubmitClient).calls).toEqual([]);
    });

    it('keeps MinIO sync errors when submit is skipped', async () => {
        const d = deps({ status: 'failed', pixivAddr: 'a.jpg', error: 'source object missing' });

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toEqual({
            status: 'skipped',
            reason: 'failed',
            error: 'source object missing',
        });
        expect((d.submitClient as FakeSubmitClient).calls).toEqual([]);
    });

    it('submits the MinIO basename and records the accepted task', async () => {
        const d = deps({
            status: 'synced',
            pixivAddr: 'a.jpg',
            ossKey: 'pixiv_img_v2/20260605/a.jpg',
            objectName: 'a.jpg',
        });

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toEqual({ status: 'submitted', taskId: 'task-1', objectName: 'a.jpg' });
        expect((d.submitClient as FakeSubmitClient).calls).toEqual([
            {
                paths: ['a.jpg'],
                callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
            },
        ]);
    });

    it('returns a retryable submit failure without making it terminal inside the HTTP boundary', async () => {
        const d = deps({
            status: 'synced',
            pixivAddr: 'a.jpg',
            ossKey: 'pixiv_img_v2/20260605/a.jpg',
            objectName: 'a.jpg',
        });
        (d.submitClient as FakeSubmitClient).nextError = new Error('entry busy');

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toEqual({
            status: 'submit_failed',
            objectName: 'a.jpg',
            error: 'entry busy',
            retryable: true,
        });
    });

    it('classifies caller errors as non-retryable without writing repository state', async () => {
        const d = deps({
            status: 'synced',
            pixivAddr: 'a.jpg',
            ossKey: 'pixiv_img_v2/20260605/a.jpg',
            objectName: 'a.jpg',
        });
        (d.submitClient as FakeSubmitClient).nextError = new TaggerSubmitError('bad callback', 400);

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toMatchObject({ status: 'submit_failed', retryable: false });
    });
});

describe('triggerTaggerForPixivAddrs', () => {
    it('submits all synced MinIO basenames as one tagger task', async () => {
        const submitClient = new FakeSubmitClient();
        const syncResults: Record<string, MinioSyncForTaggerResult> = {
            'a.jpg': {
                status: 'synced',
                pixivAddr: 'a.jpg',
                ossKey: 'pixiv_img_v2/20260605/a.jpg',
                objectName: 'a.jpg',
            },
            'b.jpg': {
                status: 'synced',
                pixivAddr: 'b.jpg',
                ossKey: 'pixiv_img_v2/20260605/b.jpg',
                objectName: 'b.jpg',
            },
        };

        const result = await triggerTaggerForPixivAddrs(['a.jpg', 'b.jpg'], {
            syncPixivToMinio: async (pixivAddr) => syncResults[pixivAddr],
            submitClient,
            callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
        });

        expect(result).toEqual({
            status: 'submitted',
            taskId: 'task-1',
            items: [
                { pixivAddr: 'a.jpg', objectName: 'a.jpg' },
                { pixivAddr: 'b.jpg', objectName: 'b.jpg' },
            ],
            skipped: [],
        });
        expect(submitClient.calls).toEqual([
            {
                paths: ['a.jpg', 'b.jpg'],
                callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
            },
        ]);
    });

    it('submits ready images and returns skipped images for retry handling', async () => {
        const submitClient = new FakeSubmitClient();
        const syncResults: Record<string, MinioSyncForTaggerResult> = {
            'a.jpg': {
                status: 'synced',
                pixivAddr: 'a.jpg',
                ossKey: 'pixiv_img_v2/20260605/a.jpg',
                objectName: 'a.jpg',
            },
            'b.jpg': {
                status: 'timeout',
                pixivAddr: 'b.jpg',
                ossKey: 'pixiv_img_v2/20260605/b.jpg',
                objectName: 'b.jpg',
                timeoutMs: 30000,
            },
        };

        const result = await triggerTaggerForPixivAddrs(['a.jpg', 'b.jpg'], {
            syncPixivToMinio: async (pixivAddr) => syncResults[pixivAddr],
            submitClient,
            callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
        });

        expect(result).toEqual({
            status: 'submitted',
            taskId: 'task-1',
            items: [{ pixivAddr: 'a.jpg', objectName: 'a.jpg' }],
            skipped: [
                {
                    pixivAddr: 'b.jpg',
                    reason: 'timeout',
                    ossKey: 'pixiv_img_v2/20260605/b.jpg',
                    objectName: 'b.jpg',
                    timeoutMs: 30000,
                },
            ],
        });
        expect(submitClient.calls).toEqual([
            {
                paths: ['a.jpg'],
                callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
            },
        ]);
    });
});
