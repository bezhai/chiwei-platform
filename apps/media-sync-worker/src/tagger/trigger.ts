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

export interface TriggerSubmittedItem {
    pixivAddr: string;
    objectName: string;
}

export type TriggerTaggerBatchResult =
    | {
        status: 'submitted';
        taskId: string;
        items: TriggerSubmittedItem[];
        skipped: TriggerSkippedItem[];
    }
    | {
        status: 'submit_failed';
        items: TriggerSubmittedItem[];
        error: string;
        skipped: TriggerSkippedItem[];
    }
    | {
        status: 'empty';
        skipped: TriggerSkippedItem[];
    };

export type TriggerSkippedItem = {
    pixivAddr: string;
    reason: Exclude<MinioSyncForTaggerResult['status'], 'synced'>;
} & TriggerSkipDetails;

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
    const batchResult = await triggerTaggerForPixivAddrs([pixivAddr], deps);
    if (batchResult.status === 'submitted') {
        return {
            status: 'submitted',
            taskId: batchResult.taskId,
            objectName: batchResult.items[0].objectName,
        };
    }
    if (batchResult.status === 'submit_failed') {
        return {
            status: 'submit_failed',
            objectName: batchResult.items[0].objectName,
            error: batchResult.error,
        };
    }

    const { pixivAddr: _pixivAddr, ...skipped } = batchResult.skipped[0];
    return {
        status: 'skipped',
        ...skipped,
    };
}

export async function triggerTaggerForPixivAddrs(
    pixivAddrs: string[],
    deps: TriggerTaggerDeps
): Promise<TriggerTaggerBatchResult> {
    const items: TriggerSubmittedItem[] = [];
    const skipped: TriggerSkippedItem[] = [];

    const syncResults = await Promise.all(pixivAddrs.map(async (pixivAddr) => ({
        pixivAddr,
        syncResult: await deps.syncPixivToMinio(pixivAddr),
    })));

    for (const { pixivAddr, syncResult } of syncResults) {
        if (syncResult.status !== 'synced') {
            skipped.push({
                pixivAddr,
                reason: syncResult.status,
                ...skipDetails(syncResult),
            });
            continue;
        }
        items.push({ pixivAddr, objectName: syncResult.objectName });
    }

    if (items.length === 0) {
        return { status: 'empty', skipped };
    }

    const objectNames = items.map((item) => item.objectName);
    try {
        const submitResult = await deps.submitClient.submit({
            paths: objectNames,
            callbackUrl: deps.callbackUrl,
        });
        await deps.repository.markSubmitted({
            taskId: submitResult.taskId,
            paths: objectNames,
        });
        return {
            status: 'submitted',
            taskId: submitResult.taskId,
            items,
            skipped,
        };
    } catch (err) {
        const error = err instanceof Error ? err.message : String(err);
        await deps.repository.markSubmitFailed({
            paths: objectNames,
            error,
        });
        return {
            status: 'submit_failed',
            items,
            error,
            skipped,
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
