import type { MinioSyncForTaggerResult } from '../storage/syncPage';
import type { TaggerSubmitResult } from './submitClient';

export interface TriggerSubmitClient {
    submit(req: { paths: string[]; callbackUrl: string }): Promise<TaggerSubmitResult>;
}

export interface TriggerRepository {
    markSubmitted(params: { taskId: string; paths: string[] }): Promise<void>;
    markSubmitFailed(params: { paths: string[]; error: string }): Promise<void>;
}

export interface TriggerTaggerDeps {
    syncPixivToMinio(pixivAddr: string): Promise<MinioSyncForTaggerResult>;
    submitClient: TriggerSubmitClient;
    repository: TriggerRepository;
    callbackUrl: string;
}

export type TriggerTaggerResult =
    | ({
        status: 'skipped';
        reason: Exclude<MinioSyncForTaggerResult['status'], 'synced'>;
    } & TriggerSkipDetails)
    | { status: 'submitted'; taskId: string; objectName: string }
    | { status: 'submit_failed'; objectName: string; error: string };

interface TriggerSkipDetails {
    objectName?: string;
    ossKey?: string;
    timeoutMs?: number;
    error?: string;
}

export async function triggerTaggerForPixivAddr(
    pixivAddr: string,
    deps: TriggerTaggerDeps
): Promise<TriggerTaggerResult> {
    const syncResult = await deps.syncPixivToMinio(pixivAddr);
    if (syncResult.status !== 'synced') {
        return {
            status: 'skipped',
            reason: syncResult.status,
            ...skipDetails(syncResult),
        };
    }

    const objectName = syncResult.objectName;
    try {
        const submitResult = await deps.submitClient.submit({
            paths: [objectName],
            callbackUrl: deps.callbackUrl,
        });
        await deps.repository.markSubmitted({
            taskId: submitResult.taskId,
            paths: [objectName],
        });
        return {
            status: 'submitted',
            taskId: submitResult.taskId,
            objectName,
        };
    } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        await deps.repository.markSubmitFailed({
            paths: [objectName],
            error,
        });
        return {
            status: 'submit_failed',
            objectName,
            error,
        };
    }
}

function skipDetails(syncResult: Exclude<MinioSyncForTaggerResult, { status: 'synced' }>): TriggerSkipDetails {
    switch (syncResult.status) {
        case 'timeout':
            return {
                objectName: syncResult.objectName,
                ossKey: syncResult.ossKey,
                timeoutMs: syncResult.timeoutMs,
            };
        case 'failed':
            return { error: syncResult.error };
        case 'disabled':
        case 'missing_key':
            return {};
    }
}
