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

    console.log(
        [
            'Tagger runtime initialized:',
            `result_mongo=on database=${resultMongoConfig.database}`,
            `callback_server=${callbackServerConfig ? `on port=${callbackServerConfig.port}` : 'off'}`,
            triggerConfig
                ? `trigger=on entry=${triggerConfig.entryUrl} callback_url=${triggerConfig.callbackUrl} timeout_ms=${triggerConfig.submitTimeoutMs} retries=${triggerConfig.submitRetries}`
                : 'trigger=off',
        ].join(' ')
    );

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

    const result = await triggerTaggerForPixivAddr(pixivAddr, {
        syncPixivToMinio: syncPixivToMinioForTagger,
        submitClient: runtimeState.submitClient,
        repository: runtimeState.resultMongo.repository,
        callbackUrl: runtimeState.triggerConfig.callbackUrl,
    });
    logTriggerResult(pixivAddr, result);
    return result;
}

function logTriggerResult(pixivAddr: string, result: TriggerTaggerResult): void {
    switch (result.status) {
        case 'submitted':
            console.log(
                `Tagger trigger submitted: pixiv_addr=${pixivAddr} object_name=${result.objectName} task_id=${result.taskId}`
            );
            return;
        case 'submit_failed':
            console.warn(
                `Tagger trigger submit failed: pixiv_addr=${pixivAddr} object_name=${result.objectName} error=${result.error}`
            );
            return;
        case 'skipped':
            console.warn(
                `Tagger trigger skipped: pixiv_addr=${pixivAddr} reason=${result.reason}${formatSkipDetails(result)}`
            );
    }
}

function formatSkipDetails(result: Extract<TriggerTaggerResult, { status: 'skipped' }>): string {
    const details: string[] = [];
    if (result.objectName) {
        details.push(`object_name=${result.objectName}`);
    }
    if (result.ossKey) {
        details.push(`oss_key=${result.ossKey}`);
    }
    if (result.timeoutMs) {
        details.push(`timeout_ms=${result.timeoutMs}`);
    }
    if (result.error) {
        details.push(`error=${result.error}`);
    }
    return details.length > 0 ? ` ${details.join(' ')}` : '';
}
