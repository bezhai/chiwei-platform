import { randomUUID } from 'node:crypto';
import { validateTaggerCallbackPayload } from './callbackServer';
import {
    TaggerTaskNotFoundError,
    type RemoteTaggerTask,
} from './submitClient';
import type { TaggerCallbackPayload, TaggerTaskDocument } from './types';

export interface SubmittedTaskReconcileConfig {
    batchSize: number;
    staleAfterMs: number;
    leaseMs: number;
    retryDelayMs: number;
    idleDelayMs?: number;
}

interface SubmittedTaskRepository {
    findDueSubmittedTasks(params: {
        limit: number;
        staleAfterMs: number;
    }): Promise<TaggerTaskDocument[]>;
    claimSubmittedTask(params: {
        taskId: string;
        leaseToken: string;
        staleAfterMs: number;
        leaseExpiresAt: Date;
    }): Promise<TaggerTaskDocument | null>;
    deferSubmittedTask(params: {
        taskId: string;
        leaseToken: string;
        nextAttemptAt: Date;
        error: string | null;
    }): Promise<void>;
    requeueSubmittedTask(params: {
        taskId: string;
        leaseToken: string;
        error: string;
        nextAttemptAt: Date;
    }): Promise<void>;
    finishRegisteringTask(params: {
        taskId: string;
        leaseToken: string;
    }): Promise<void>;
    applyCallback(payload: TaggerCallbackPayload): Promise<void>;
}

interface SubmittedTaskClient {
    getTask(taskId: string): Promise<RemoteTaggerTask>;
}

export interface SubmittedTaskReconcileDeps {
    repository: SubmittedTaskRepository;
    taskClient: SubmittedTaskClient;
    config: SubmittedTaskReconcileConfig;
    now?: () => Date;
    leaseToken?: () => string;
    sleep?: (ms: number) => Promise<void>;
}

export interface SubmittedTaskReconcileWorker {
    stop(): Promise<void>;
}

export async function processSubmittedTaskReconciliation(
    deps: SubmittedTaskReconcileDeps
): Promise<number> {
    const now = deps.now ?? (() => new Date());
    const docs = await deps.repository.findDueSubmittedTasks({
        limit: deps.config.batchSize,
        staleAfterMs: deps.config.staleAfterMs,
    });

    for (const doc of docs) {
        const leaseToken = (deps.leaseToken ?? randomUUID)();
        const claimed = await deps.repository.claimSubmittedTask({
            taskId: doc.task_id,
            leaseToken,
            staleAfterMs: deps.config.staleAfterMs,
            leaseExpiresAt: new Date(now().getTime() + deps.config.leaseMs),
        });
        if (!claimed) continue;

        const nextAttemptAt = new Date(now().getTime() + deps.config.retryDelayMs);
        if (claimed.status === 'registering') {
            await deps.repository.finishRegisteringTask({
                taskId: claimed.task_id,
                leaseToken,
            });
            continue;
        }
        if (claimed.status === 'requeueing') {
            await deps.repository.requeueSubmittedTask({
                taskId: claimed.task_id,
                leaseToken,
                error: claimed.error ?? 'resuming interrupted remote task requeue',
                nextAttemptAt,
            });
            continue;
        }
        try {
            const remote = await deps.taskClient.getTask(claimed.task_id);
            switch (remote.status) {
                case 'accepted':
                case 'running':
                    await deps.repository.deferSubmittedTask({
                        taskId: claimed.task_id,
                        leaseToken,
                        nextAttemptAt,
                        error: null,
                    });
                    break;
                case 'pending_callback':
                case 'completed':
                    await applyRemoteResult(remote, claimed.task_id, deps.repository);
                    break;
                case 'failed':
                    if (remote.result) {
                        await applyRemoteResult(remote, claimed.task_id, deps.repository);
                    } else {
                        await deps.repository.requeueSubmittedTask({
                            taskId: claimed.task_id,
                            leaseToken,
                            error: remote.error ?? 'remote tagger task failed without result',
                            nextAttemptAt,
                        });
                    }
                    break;
            }
        } catch (err) {
            const error = err instanceof Error ? err.message : String(err);
            if (err instanceof TaggerTaskNotFoundError) {
                await deps.repository.requeueSubmittedTask({
                    taskId: claimed.task_id,
                    leaseToken,
                    error,
                    nextAttemptAt,
                });
            } else {
                console.warn(
                    `Submitted tagger task reconciliation deferred: task_id=${claimed.task_id} error=${error}`
                );
                await deps.repository.deferSubmittedTask({
                    taskId: claimed.task_id,
                    leaseToken,
                    nextAttemptAt,
                    error,
                });
            }
        }
    }

    return docs.length;
}

async function applyRemoteResult(
    remote: RemoteTaggerTask,
    expectedTaskId: string,
    repository: SubmittedTaskRepository
): Promise<void> {
    if (!remote.result) {
        throw new Error(`tagger task ${expectedTaskId} has status ${remote.status} without result`);
    }
    if (remote.result.task_id !== expectedTaskId) {
        throw new Error('tagger task result task_id mismatch');
    }
    await repository.applyCallback(validateTaggerCallbackPayload(remote.result));
}

export function startSubmittedTaskReconcileWorker(
    deps: SubmittedTaskReconcileDeps
): SubmittedTaskReconcileWorker {
    let stopped = false;
    const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    const running = (async () => {
        while (!stopped) {
            try {
                const processed = await processSubmittedTaskReconciliation(deps);
                if (processed === 0) await sleep(deps.config.idleDelayMs ?? 5000);
            } catch (err) {
                console.error('Submitted tagger task reconciliation failed:', err);
                await sleep(deps.config.idleDelayMs ?? 5000);
            }
        }
    })();

    return {
        async stop() {
            stopped = true;
            await running;
        },
    };
}
