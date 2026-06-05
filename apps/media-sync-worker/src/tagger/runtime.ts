import { bestEffortSyncToMinio, syncPixivToMinioForTagger } from '../storage/syncPage';
import {
    loadTaggerCallbackServerConfig,
    loadTaggerResultMongoConfig,
    loadTaggerTriggerConfig,
    type TaggerTriggerConfig,
} from './config';
import { createTaggerResultMongo, type TaggerResultMongo } from './resultMongo';
import { startTaggerCallbackServer } from './callbackServer';
import { TaggerSubmitClient } from './submitClient';
import { triggerTaggerForPixivAddr, type TriggerTaggerResult } from './trigger';

interface TaggerRuntimeState {
    resultMongo: TaggerResultMongo;
    triggerConfig: TaggerTriggerConfig | null;
    submitClient: TaggerSubmitClient | null;
    callbackServer: { stop: () => void } | null;
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
        console.log('Tagger runtime disabled: TAGGER_RESULT_MONGO_ENABLED is off.');
        return;
    }

    const resultMongo = await createTaggerResultMongo(resultMongoConfig);
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

    runtimeState = {
        resultMongo,
        triggerConfig,
        submitClient,
        callbackServer,
    };
}

export async function stopTaggerRuntime(): Promise<void> {
    runtimeState?.callbackServer?.stop();
    await runtimeState?.resultMongo.service.close();
    runtimeState = null;
}

export async function syncMinioAndMaybeSubmitTagger(pixivAddr: string): Promise<TriggerTaggerResult | { status: 'tagger_disabled' }> {
    if (!runtimeState?.triggerConfig || !runtimeState.submitClient) {
        await bestEffortSyncToMinio(pixivAddr);
        return { status: 'tagger_disabled' };
    }

    return triggerTaggerForPixivAddr(pixivAddr, {
        syncPixivToMinio: syncPixivToMinioForTagger,
        submitClient: runtimeState.submitClient,
        repository: runtimeState.resultMongo.repository,
        callbackUrl: runtimeState.triggerConfig.callbackUrl,
    });
}
