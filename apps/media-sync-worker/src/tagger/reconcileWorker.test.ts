import { describe, expect, it } from 'bun:test';
import { processSubmittedTaskReconciliation } from './reconcileWorker';
import { TaggerTaskNotFoundError } from './submitClient';
import type { TaggerTaskDocument } from './types';

const now = new Date('2026-07-22T08:00:00.000Z');

function task(): TaggerTaskDocument {
    return {
        task_id: 'task-1',
        paths: ['a.jpg'],
        image_generations: { 'a.jpg': 2 },
        status: 'submitted',
        submitted_at: new Date('2026-07-22T07:00:00.000Z'),
        created_at: now,
        updated_at: now,
    };
}

class FakeRepository {
    docs = [task()];
    claimResult: TaggerTaskDocument | null | undefined;
    deferred: Array<Record<string, unknown>> = [];
    requeued: Array<Record<string, unknown>> = [];
    finishedRegistrations: Array<Record<string, unknown>> = [];
    callbacks: unknown[] = [];

    async findDueSubmittedTasks() {
        return this.docs;
    }

    async claimSubmittedTask(params: Record<string, unknown>) {
        if (this.claimResult !== undefined) return this.claimResult;
        return { ...this.docs[0], reconcile_lease_token: params.leaseToken as string };
    }

    async deferSubmittedTask(params: Record<string, unknown>) {
        this.deferred.push(params);
    }

    async requeueSubmittedTask(params: Record<string, unknown>) {
        this.requeued.push(params);
    }

    async finishRegisteringTask(params: Record<string, unknown>) {
        this.finishedRegistrations.push(params);
    }

    async applyCallback(payload: unknown) {
        this.callbacks.push(payload);
    }
}

function remote(status: 'accepted' | 'running' | 'pending_callback' | 'completed' | 'failed', result: Record<string, unknown> | null = null) {
    return { taskId: 'task-1', status, paths: ['a.jpg'], result, error: status === 'failed' ? 'failed' : null };
}

const config = {
    batchSize: 4,
    staleAfterMs: 600_000,
    leaseMs: 60_000,
    retryDelayMs: 120_000,
};

describe('processSubmittedTaskReconciliation', () => {
    it.each(['accepted', 'running'] as const)('defers an active remote task in status %s', async (status) => {
        const repository = new FakeRepository();

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => remote(status) },
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
        });

        expect(repository.deferred).toEqual([{
            taskId: 'task-1',
            leaseToken: 'lease-1',
            nextAttemptAt: new Date('2026-07-22T08:02:00.000Z'),
            error: null,
        }]);
        expect(repository.requeued).toEqual([]);
    });

    it.each(['pending_callback', 'completed', 'failed'] as const)(
        'applies the preserved callback result for remote status %s',
        async (status) => {
            const repository = new FakeRepository();
            const payload = { task_id: 'task-1', status: 'completed', rows: [{ id: 'a.jpg', schema_version: 1 }] };

            await processSubmittedTaskReconciliation({
                repository: repository as any,
                taskClient: { getTask: async () => remote(status, payload) },
                config,
                now: () => now,
                leaseToken: () => 'lease-1',
            });

            expect(repository.callbacks).toEqual([payload]);
            expect(repository.requeued).toEqual([]);
        },
    );

    it('starts a new generation when the remote task failed without a result', async () => {
        const repository = new FakeRepository();

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => remote('failed') },
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
        });

        expect(repository.requeued).toEqual([{
            taskId: 'task-1',
            leaseToken: 'lease-1',
            error: 'failed',
            nextAttemptAt: new Date('2026-07-22T08:02:00.000Z'),
        }]);
    });

    it('finishes an interrupted task registration before any remote lookup', async () => {
        const repository = new FakeRepository();
        repository.claimResult = {
            ...task(),
            status: 'registering',
            registering_at: new Date('2026-07-22T07:00:00.000Z'),
            image_processing_leases: { 'a.jpg': 'trigger-owner' },
            reconcile_lease_token: 'lease-1',
        };
        let fetched = false;

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => { fetched = true; return remote('running'); } },
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
        });

        expect(fetched).toBeFalse();
        expect(repository.finishedRegistrations).toEqual([{
            taskId: 'task-1',
            leaseToken: 'lease-1',
        }]);
    });

    it('resumes an interrupted requeue without querying the remote task again', async () => {
        const repository = new FakeRepository();
        repository.claimResult = {
            ...task(),
            status: 'requeueing',
            error: 'remote task missing',
            reconcile_lease_token: 'lease-1',
        };
        let fetched = false;

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => { fetched = true; return remote('running'); } },
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
        });

        expect(fetched).toBeFalse();
        expect(repository.requeued[0]).toMatchObject({
            taskId: 'task-1',
            leaseToken: 'lease-1',
            error: 'remote task missing',
        });
    });

    it('requeues a remote task that is explicitly gone', async () => {
        const repository = new FakeRepository();

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => { throw new TaggerTaskNotFoundError('task-1'); } },
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
        });

        expect(repository.requeued[0]).toMatchObject({
            taskId: 'task-1',
            error: 'tagger task not found: task-1',
        });
    });

    it('defers transport and protocol failures without creating a duplicate remote task', async () => {
        const repository = new FakeRepository();

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => { throw new Error('connection reset'); } },
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
        });

        expect(repository.requeued).toEqual([]);
        expect(repository.deferred[0]).toMatchObject({
            taskId: 'task-1',
            error: 'connection reset',
        });
    });

    it('rejects malformed preserved callback payload and defers reconciliation', async () => {
        const repository = new FakeRepository();

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => remote('completed', { task_id: 'other', status: 'completed', rows: [] }) },
            config,
            now: () => now,
            leaseToken: () => 'lease-1',
        });

        expect(repository.callbacks).toEqual([]);
        expect(repository.deferred[0]).toMatchObject({ error: 'tagger task result task_id mismatch' });
    });

    it('does nothing when another worker owns the submitted-task lease', async () => {
        const repository = new FakeRepository();
        repository.claimResult = null;
        let fetched = false;

        await processSubmittedTaskReconciliation({
            repository: repository as any,
            taskClient: { getTask: async () => { fetched = true; return remote('running'); } },
            config,
        });

        expect(fetched).toBe(false);
    });
});
