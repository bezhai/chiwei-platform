import { describe, expect, it } from 'bun:test';
import { processTaggerProjectionBatch } from './projectionWorker';
import type { TaggerImageResultDocument } from './types';

const now = new Date('2026-07-22T08:00:00.000Z');

function resultDoc(): TaggerImageResultDocument {
    return {
        pixiv_addr: 'a.jpg',
        object_name: 'a.jpg',
        task_id: 'task-1',
        generation: 2,
        status: 'completed',
        result: { id: 'a.jpg', schema_version: 1, future: { value: 'kept' } },
        projection_status: 'pending',
        projection_attempts: 0,
        created_at: now,
        updated_at: now,
    };
}

class FakeProjectionRepository {
    docs = [resultDoc()];
    claimed: Array<Record<string, unknown>> = [];
    completed: Array<Record<string, unknown>> = [];
    retries: Array<Record<string, unknown>> = [];
    claimResult: TaggerImageResultDocument | null | undefined;
    includeHistorical: boolean | undefined;

    async findDueProjectionImages(params: { includeHistorical: boolean }) {
        this.includeHistorical = params.includeHistorical;
        return this.docs;
    }

    async claimProjection(params: Record<string, unknown>) {
        this.claimed.push(params);
        if (this.claimResult !== undefined) return this.claimResult;
        return { ...this.docs[0], projection_lease_token: params.leaseToken as string };
    }

    async markProjectionCompleted(params: Record<string, unknown>) {
        this.completed.push(params);
    }

    async markProjectionRetry(params: Record<string, unknown>) {
        this.retries.push(params);
    }
}

const config = {
    batchSize: 4,
    processingTimeoutMs: 600_000,
    retryDelayMs: 60_000,
    includeHistorical: false,
};

describe('processTaggerProjectionBatch', () => {
    it('projects raw results and completes with generation, owner and lease fencing', async () => {
        const repository = new FakeProjectionRepository();
        const projected: unknown[] = [];

        const processed = await processTaggerProjectionBatch({
            repository: repository as any,
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
            projectResult: async (params) => {
                projected.push(params);
                return 2;
            },
        });

        expect(processed).toBe(1);
        expect(projected).toEqual([{
            pixivAddr: 'a.jpg',
            taskId: 'task-1',
            generation: 2,
            status: 'completed',
            result: resultDoc().result,
        }]);
        expect(repository.completed).toEqual([{
            path: 'a.jpg', taskId: 'task-1', generation: 2, leaseToken: 'lease-1', projectedAt: now,
        }]);
        expect(repository.retries).toEqual([]);
    });

    it('retries instead of creating a metadata-free image when no local source matches', async () => {
        const repository = new FakeProjectionRepository();

        await processTaggerProjectionBatch({
            repository: repository as any,
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
            projectResult: async () => 0,
        });

        expect(repository.completed).toEqual([]);
        expect(repository.retries).toEqual([{
            path: 'a.jpg',
            taskId: 'task-1',
            generation: 2,
            leaseToken: 'lease-1',
            attempts: 1,
            error: 'no local pixiv image matched projection',
            nextAttemptAt: new Date('2026-07-22T08:01:00.000Z'),
        }]);
    });

    it('does nothing when another worker owns the projection claim', async () => {
        const repository = new FakeProjectionRepository();
        repository.claimResult = null;
        let projected = false;

        await processTaggerProjectionBatch({
            repository: repository as any,
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
            projectResult: async () => {
                projected = true;
                return 1;
            },
        });

        expect(projected).toBe(false);
        expect(repository.completed).toEqual([]);
    });

    it('passes the separately controlled historical backfill flag to the due query', async () => {
        const repository = new FakeProjectionRepository();
        repository.docs = [];

        await processTaggerProjectionBatch({
            repository: repository as any,
            config: { ...config, includeHistorical: true },
            projectResult: async () => 1,
        });

        expect(repository.includeHistorical).toBe(true);
    });
});
