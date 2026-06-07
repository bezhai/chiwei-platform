import { bestEffortSyncToMinio } from '../storage/syncPage';
import {
    loadTaggerCallbackServerConfig,
    loadTaggerResultMongoConfig,
    loadTaggerTriggerConfig,
    type TaggerTriggerConfig,
} from './config';
import { createTaggerResultMongo, type TaggerResultMongo } from './resultMongo';
import { startTaggerCallbackServer } from './callbackServer';
import { TaggerSubmitClient } from './submitClient';
import { startTaggerTriggerWorker, type TaggerTriggerWorker } from './triggerWorker';

interface TaggerRuntimeState {
    resultMongo: TaggerResultMongo;
    triggerConfig: TaggerTriggerConfig | null;
    submitClient: TaggerSubmitClient | null;
    callbackServer: { stop: () => void } | null;
    triggerWorker: TaggerTriggerWorker | null;
}

let runtimeState: TaggerRuntimeState | null = null;

export async function initTaggerRuntime(): Promise<void> {
    const resultMongoConfig = loadTaggerResultMongoConfig();
    const triggerConfig = loadTaggerTriggerConfig();
    const callbackServerConfig = loadTaggerCallbackServerConfig();

    if (!resultMongoConfig) {
        if (triggerConfig || callbackServerConfig) {
            throw new Error('TAGGER_RESULT_MONGO_* must be configured when tagger trigger or callback server is enabled');
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

    console.log(
        [
            'Tagger runtime initialized:',
            `result_mongo=on database=${resultMongoConfig.database}`,
            `callback_server=${callbackServerConfig ? `on port=${callbackServerConfig.port}` : 'off'}`,
            triggerConfig
                ? `trigger=on entry=${triggerConfig.entryUrl} callback_url=${triggerConfig.callbackUrl} timeout_ms=${triggerConfig.submitTimeoutMs} retries=${triggerConfig.submitRetries} worker=on retry_delay_ms=${triggerConfig.retryDelayMs} max_attempts=${triggerConfig.maxAttempts}`
                : 'trigger=off',
        ].join(' ')
    );

    runtimeState = {
        resultMongo,
        triggerConfig,
        submitClient,
        callbackServer,
        triggerWorker,
    };
}

export async function stopTaggerRuntime(): Promise<void> {
    await runtimeState?.triggerWorker?.stop();
    runtimeState?.callbackServer?.stop();
    await runtimeState?.resultMongo.service.close();
    runtimeState = null;
}

export async function syncMinioAndMaybeSubmitTagger(pixivAddr: string): Promise<{ status: 'queued' } | { status: 'enqueue_failed'; error: string } | { status: 'tagger_disabled' }> {
    if (!runtimeState?.triggerConfig || !runtimeState.submitClient) {
        await bestEffortSyncToMinio(pixivAddr);
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
