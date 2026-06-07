import { syncPixivToMinioForTagger, type MinioSyncForTaggerResult } from '../storage/syncPage';
import type { TaggerSubmitClient } from './submitClient';
import { triggerTaggerForPixivAddr, type TriggerTaggerResult } from './trigger';
import type { TaggerTriggerConfig } from './config';
import type { TaggerResultRepository } from './resultRepository';
import type { TaggerImageResultDocument } from './types';

export interface TaggerTriggerWorker {
    stop(): Promise<void>;
}

export interface TaggerTriggerWorkerDeps {
    repository: TaggerResultRepository;
    submitClient: TaggerSubmitClient;
    config: TaggerTriggerConfig;
    syncPixivToMinio?: (pixivAddr: string) => Promise<MinioSyncForTaggerResult>;
    sleep?: (ms: number) => Promise<void>;
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export function startTaggerTriggerWorker(deps: TaggerTriggerWorkerDeps): TaggerTriggerWorker {
    let stopped = false;
    const sleep = deps.sleep ?? defaultSleep;

    const loop = async () => {
        console.log(
            `Tagger trigger worker started: batch_size=${deps.config.batchSize} idle_delay_ms=${deps.config.workerIdleDelayMs} retry_delay_ms=${deps.config.retryDelayMs} max_attempts=${deps.config.maxAttempts}`
        );
        while (!stopped) {
            try {
                const processed = await processTaggerTriggerBatch(deps);
                if (processed === 0) {
                    await sleep(deps.config.workerIdleDelayMs);
                }
            } catch (err) {
                console.error('Tagger trigger worker batch failed:', err);
                await sleep(deps.config.workerIdleDelayMs);
            }
        }
        console.log('Tagger trigger worker stopped.');
    };

    const running = loop();

    return {
        async stop() {
            stopped = true;
            await running;
        },
    };
}

export async function processTaggerTriggerBatch(deps: TaggerTriggerWorkerDeps): Promise<number> {
    const docs = await deps.repository.findDueTriggerImages({
        limit: deps.config.batchSize,
        processingTimeoutMs: deps.config.processingTimeoutMs,
    });
    for (const doc of docs) {
        await processOne(doc, deps);
    }
    return docs.length;
}

async function processOne(doc: TaggerImageResultDocument, deps: TaggerTriggerWorkerDeps): Promise<void> {
    const path = doc.pixiv_addr;
    const claimedDoc = await deps.repository.claimDueTriggerImage({
        path,
        processingTimeoutMs: deps.config.processingTimeoutMs,
    });
    if (!claimedDoc) {
        console.log(`Tagger trigger claim skipped: pixiv_addr=${path}`);
        return;
    }

    const result = await triggerTaggerForPixivAddr(path, {
        syncPixivToMinio: deps.syncPixivToMinio ?? syncPixivToMinioForTagger,
        submitClient: deps.submitClient,
        repository: deps.repository,
        callbackUrl: deps.config.callbackUrl,
    });

    if (result.status === 'submitted') {
        console.log(`Tagger trigger submitted: pixiv_addr=${path} object_name=${result.objectName} task_id=${result.taskId}`);
        return;
    }

    const attempts = (claimedDoc.attempts ?? 0) + 1;
    const error = formatTriggerError(result);
    if (attempts >= deps.config.maxAttempts || (result.status === 'skipped' && result.reason === 'disabled')) {
        await deps.repository.markSubmitFailed({ paths: [path], error });
        console.warn(`Tagger trigger exhausted: pixiv_addr=${path} attempts=${attempts} error=${error}`);
        return;
    }

    const nextAttemptAt = new Date(Date.now() + deps.config.retryDelayMs);
    await deps.repository.markRetry({
        path,
        error,
        attempts,
        nextAttemptAt,
    });
    console.warn(
        `Tagger trigger will retry: pixiv_addr=${path} attempts=${attempts}/${deps.config.maxAttempts} next_attempt_at=${nextAttemptAt.toISOString()} error=${error}`
    );
}

function formatTriggerError(result: Exclude<TriggerTaggerResult, { status: 'submitted' }>): string {
    if (result.status === 'submit_failed') {
        return result.error;
    }
    const details = [
        `reason=${result.reason}`,
        result.objectName ? `object_name=${result.objectName}` : '',
        result.ossKey ? `oss_key=${result.ossKey}` : '',
        result.timeoutMs ? `timeout_ms=${result.timeoutMs}` : '',
        result.error ? `error=${result.error}` : '',
    ].filter(Boolean);
    return details.join(' ');
}
