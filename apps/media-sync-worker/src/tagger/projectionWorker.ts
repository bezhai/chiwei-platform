import { randomUUID } from 'node:crypto';
import { projectTaggerResultToLocal, type TaggerProjectionParams } from '../mongo/taggerProjection';
import type { TaggerImageResultDocument } from './types';

export interface TaggerProjectionWorkerConfig {
    batchSize: number;
    processingTimeoutMs: number;
    retryDelayMs: number;
    includeHistorical: boolean;
    idleDelayMs?: number;
}

interface ProjectionRepository {
    findDueProjectionImages(params: {
        limit: number;
        processingTimeoutMs: number;
        includeHistorical: boolean;
    }): Promise<TaggerImageResultDocument[]>;
    claimProjection(params: {
        path: string;
        taskId: string;
        generation: number;
        leaseToken: string;
        processingTimeoutMs: number;
        includeHistorical: boolean;
    }): Promise<TaggerImageResultDocument | null>;
    markProjectionCompleted(params: {
        path: string;
        taskId: string;
        generation: number;
        leaseToken: string;
        projectedAt: Date;
    }): Promise<void>;
    markProjectionRetry(params: {
        path: string;
        taskId: string;
        generation: number;
        leaseToken: string;
        attempts: number;
        error: string;
        nextAttemptAt: Date;
    }): Promise<void>;
}

export interface TaggerProjectionWorkerDeps {
    repository: ProjectionRepository;
    config: TaggerProjectionWorkerConfig;
    projectResult?: (params: TaggerProjectionParams) => Promise<number>;
    now?: () => Date;
    leaseToken?: () => string;
    sleep?: (ms: number) => Promise<void>;
}

export interface TaggerProjectionWorker {
    stop(): Promise<void>;
}

export async function processTaggerProjectionBatch(
    deps: TaggerProjectionWorkerDeps
): Promise<number> {
    const now = deps.now ?? (() => new Date());
    const docs = await deps.repository.findDueProjectionImages({
        limit: deps.config.batchSize,
        processingTimeoutMs: deps.config.processingTimeoutMs,
        includeHistorical: deps.config.includeHistorical,
    });

    for (const doc of docs) {
        const taskId = doc.task_id;
        const generation = doc.generation ?? 0;
        if (!taskId) {
            console.warn(`Tagger projection skipped malformed result without task_id: pixiv_addr=${doc.pixiv_addr}`);
            continue;
        }
        const leaseToken = (deps.leaseToken ?? randomUUID)();
        const claimed = await deps.repository.claimProjection({
            path: doc.pixiv_addr,
            taskId,
            generation,
            leaseToken,
            processingTimeoutMs: deps.config.processingTimeoutMs,
            includeHistorical: deps.config.includeHistorical,
        });
        if (!claimed) continue;

        try {
            if (!claimed.result) {
                throw new Error('tagger image result is missing raw result');
            }
            const matched = await (deps.projectResult ?? projectTaggerResultToLocal)({
                pixivAddr: claimed.pixiv_addr,
                taskId,
                generation,
                status: claimed.status,
                result: claimed.result,
            });
            if (matched === 0) {
                throw new Error('no local pixiv image matched projection');
            }
            await deps.repository.markProjectionCompleted({
                path: claimed.pixiv_addr,
                taskId,
                generation,
                leaseToken,
                projectedAt: now(),
            });
        } catch (err) {
            const attempts = (claimed.projection_attempts ?? 0) + 1;
            await deps.repository.markProjectionRetry({
                path: claimed.pixiv_addr,
                taskId,
                generation,
                leaseToken,
                attempts,
                error: err instanceof Error ? err.message : String(err),
                nextAttemptAt: new Date(now().getTime() + deps.config.retryDelayMs),
            });
            console.warn(
                `Tagger projection will retry: pixiv_addr=${claimed.pixiv_addr} task_id=${taskId} generation=${generation} attempts=${attempts} error=${err instanceof Error ? err.message : String(err)}`
            );
        }
    }

    return docs.length;
}

export function startTaggerProjectionWorker(
    deps: TaggerProjectionWorkerDeps
): TaggerProjectionWorker {
    let stopped = false;
    const sleep = deps.sleep ?? ((ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    const running = (async () => {
        while (!stopped) {
            try {
                const processed = await processTaggerProjectionBatch(deps);
                if (processed === 0) await sleep(deps.config.idleDelayMs ?? 5000);
            } catch (err) {
                console.error('Tagger projection worker batch failed:', err);
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
