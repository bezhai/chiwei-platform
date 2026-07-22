import { syncPixivToMinioForTagger } from '../storage/syncPage';
import {
    loadTaggerCallbackServerConfig,
    loadTaggerProjectionConfig,
    loadTaggerResultMongoConfig,
    loadTaggerTriggerConfig,
    type TaggerTriggerConfig,
} from './config';
import { createTaggerResultMongo, type TaggerResultMongo } from './resultMongo';
import { startTaggerCallbackServer } from './callbackServer';
import { TaggerSubmitClient } from './submitClient';
import { startTaggerTriggerWorker, type TaggerTriggerWorker } from './triggerWorker';
import { startTaggerProjectionWorker, type TaggerProjectionWorker } from './projectionWorker';
import {
    startSubmittedTaskReconcileWorker,
    type SubmittedTaskReconcileWorker,
} from './reconcileWorker';

interface TaggerRuntimeState {
    resultMongo: TaggerResultMongo;
    triggerConfig: TaggerTriggerConfig | null;
    submitClient: TaggerSubmitClient | null;
    callbackServer: { stop: () => void } | null;
    triggerWorker: TaggerTriggerWorker | null;
    projectionWorker: TaggerProjectionWorker | null;
    reconcileWorker: SubmittedTaskReconcileWorker | null;
}

let runtimeState: TaggerRuntimeState | null = null;

export async function initTaggerRuntime(): Promise<void> {
    const resultMongoConfig = loadTaggerResultMongoConfig();
    const triggerConfig = loadTaggerTriggerConfig();
    const projectionConfig = loadTaggerProjectionConfig();
    const callbackServerConfig = loadTaggerCallbackServerConfig();

    validateTaggerFeatureFlags({
        triggerEnabled: triggerConfig !== null,
        projectionEnabled: projectionConfig !== null,
    });

    if (!resultMongoConfig) {
        if (triggerConfig || callbackServerConfig || projectionConfig) {
            throw new Error(
                'TAGGER_RESULT_MONGO_* must be configured when tagger trigger, projection, or callback server is enabled'
            );
        }
        console.log('Tagger runtime disabled: result_mongo=off callback_server=off trigger=off');
        return;
    }

    const resultMongo = await createTaggerResultMongo(resultMongoConfig);
    console.log(
        `Tagger result Mongo ready: host=${resultMongoConfig.host} database=${resultMongoConfig.database}`
    );
    const callbackServer = callbackServerConfig
        ? startTaggerCallbackServer(resultMongo.repository, callbackServerConfig)
        : null;
    const submitClient = triggerConfig
        ? new TaggerSubmitClient({
            entryUrl: triggerConfig.entryUrl,
            apiToken: triggerConfig.apiToken,
            timeoutMs: triggerConfig.submitTimeoutMs,
            retries: triggerConfig.submitRetries,
        })
        : null;
    const triggerWorker = triggerConfig && submitClient
        ? startTaggerTriggerWorker({
            repository: resultMongo.repository,
            submitClient,
            config: triggerConfig,
        })
        : null;
    const reconcileWorker = triggerConfig && submitClient
        ? startSubmittedTaskReconcileWorker({
            repository: resultMongo.repository,
            taskClient: submitClient,
            config: {
                batchSize: triggerConfig.batchSize,
                staleAfterMs: triggerConfig.reconcileAfterMs,
                leaseMs: triggerConfig.reconcileLeaseMs,
                retryDelayMs: triggerConfig.reconcileRetryDelayMs,
                idleDelayMs: triggerConfig.workerIdleDelayMs,
            },
        })
        : null;
    const projectionWorker = projectionConfig
        ? startTaggerProjectionWorker({
            repository: resultMongo.repository,
            config: {
                ...projectionConfig,
                idleDelayMs: projectionConfig.workerIdleDelayMs,
            },
        })
        : null;

    console.log(
        [
            'Tagger runtime initialized:',
            `result_mongo=on database=${resultMongoConfig.database}`,
            `callback_server=${callbackServerConfig ? `on port=${callbackServerConfig.port}` : 'off'}`,
            triggerConfig
                ? `trigger=on entry=${triggerConfig.entryUrl} callback_url=${triggerConfig.callbackUrl} timeout_ms=${triggerConfig.submitTimeoutMs} retries=${triggerConfig.submitRetries} worker=on retry_delay_ms=${triggerConfig.retryDelayMs} max_attempts=${triggerConfig.maxAttempts} reconcile=on`
                : 'trigger=off',
            projectionConfig
                ? `projection=on historical_backfill=${projectionConfig.includeHistorical}`
                : 'projection=off',
        ].join(' ')
    );

    runtimeState = {
        resultMongo,
        triggerConfig,
        submitClient,
        callbackServer,
        triggerWorker,
        projectionWorker,
        reconcileWorker,
    };
}

export async function stopTaggerRuntime(): Promise<void> {
    await runtimeState?.triggerWorker?.stop();
    await runtimeState?.reconcileWorker?.stop();
    await runtimeState?.projectionWorker?.stop();
    runtimeState?.callbackServer?.stop();
    await runtimeState?.resultMongo.service.close();
    runtimeState = null;
}

export async function syncMinioAndMaybeSubmitTagger(pixivAddr: string): Promise<{ status: 'queued' } | { status: 'enqueue_failed'; error: string } | { status: 'tagger_disabled' }> {
    if (!runtimeState?.triggerConfig || !runtimeState.submitClient) {
        const result = await syncPixivToMinioForTagger(pixivAddr);
        switch (result.status) {
            case 'disabled':
            case 'synced':
                break;
            case 'missing_key':
                throw new Error(`MinIO sync missing source key for ${pixivAddr}`);
            case 'timeout':
                throw new Error(`MinIO sync timed out for ${pixivAddr} after ${result.timeoutMs}ms`);
            case 'failed':
                throw new Error(`MinIO sync failed for ${pixivAddr}: ${result.error}`);
        }
        return { status: 'tagger_disabled' };
    }

    try {
        await runtimeState.resultMongo.repository.enqueueForTrigger(pixivAddr);
        console.log(`Tagger trigger queued: pixiv_addr=${pixivAddr}`);
        return { status: 'queued' };
    } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        console.warn(`Tagger trigger enqueue failed: pixiv_addr=${pixivAddr} error=${error}`);
        return { status: 'enqueue_failed', error };
    }
}

type RuntimeFlagEnv = Record<string, string | undefined>;

export function validateTaggerFeatureFlags(
    features: { triggerEnabled: boolean; projectionEnabled: boolean },
    env: RuntimeFlagEnv = process.env
): void {
    if (features.triggerEnabled && !isEnabled(env.MINIO_SYNC_ENABLED)) {
        throw new Error('MINIO_SYNC_ENABLED must be true when TAGGER_TRIGGER_ENABLED is true');
    }
    if (
        (features.triggerEnabled || features.projectionEnabled)
        && !isEnabled(env.PIXIV_IMAGE_MIRROR_MONGO_ENABLED)
    ) {
        throw new Error(
            'PIXIV_IMAGE_MIRROR_MONGO_ENABLED must be true when tagger trigger or projection is enabled'
        );
    }
}

function isEnabled(value: string | undefined): boolean {
    return value === '1' || value?.toLowerCase() === 'true';
}
