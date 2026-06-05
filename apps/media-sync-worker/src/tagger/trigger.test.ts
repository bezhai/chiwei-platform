import { describe, expect, it } from 'bun:test';
import { triggerTaggerForPixivAddr, type TriggerTaggerDeps } from './trigger';
import type { MinioSyncForTaggerResult } from '../storage/syncPage';

class FakeSubmitClient {
    calls: Array<{ paths: string[]; callbackUrl: string }> = [];
    nextResult = { taskId: 'task-1', status: 'accepted' };
    nextError: Error | null = null;

    async submit(req: { paths: string[]; callbackUrl: string }) {
        this.calls.push(req);
        if (this.nextError) {
            throw this.nextError;
        }
        return this.nextResult;
    }
}

class FakeRepo {
    submitted: Array<{ taskId: string; paths: string[] }> = [];
    failed: Array<{ paths: string[]; error: string }> = [];

    async markSubmitted(params: { taskId: string; paths: string[] }) {
        this.submitted.push(params);
    }

    async markSubmitFailed(params: { paths: string[]; error: string }) {
        this.failed.push(params);
    }
}

function deps(syncResult: MinioSyncForTaggerResult): TriggerTaggerDeps {
    const submitClient = new FakeSubmitClient();
    const repository = new FakeRepo();
    return {
        syncPixivToMinio: async () => syncResult,
        submitClient,
        repository,
        callbackUrl: 'http://media-sync-worker/internal/tagger/callback',
    };
}

describe('triggerTaggerForPixivAddr', () => {
    it('skips submit when MinIO is not synced', async () => {
        const d = deps({ status: 'missing_key', pixivAddr: 'a.jpg' });

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toEqual({ status: 'skipped', reason: 'missing_key' });
        expect((d.submitClient as FakeSubmitClient).calls).toEqual([]);
        expect((d.repository as FakeRepo).submitted).toEqual([]);
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
        expect((d.repository as FakeRepo).submitted).toEqual([{ taskId: 'task-1', paths: ['a.jpg'] }]);
    });

    it('records submit failures locally and does not throw', async () => {
        const d = deps({
            status: 'synced',
            pixivAddr: 'a.jpg',
            ossKey: 'pixiv_img_v2/20260605/a.jpg',
            objectName: 'a.jpg',
        });
        (d.submitClient as FakeSubmitClient).nextError = new Error('entry busy');

        const result = await triggerTaggerForPixivAddr('a.jpg', d);

        expect(result).toEqual({ status: 'submit_failed', objectName: 'a.jpg', error: 'entry busy' });
        expect((d.repository as FakeRepo).failed).toEqual([{ paths: ['a.jpg'], error: 'entry busy' }]);
    });
});
