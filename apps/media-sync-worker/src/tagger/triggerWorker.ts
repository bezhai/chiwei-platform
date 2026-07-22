import { randomUUID } from 'node:crypto';
import { syncPixivToMinioForTagger, type MinioSyncForTaggerResult } from '../storage/syncPage';
import type { TaggerSubmitClient } from './submitClient';
import {
    triggerTaggerForPixivAddrs,
    type TriggerSkippedItem,
} from './trigger';
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
    leaseToken?: () => string;
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
    const claimedDocs: TaggerImageResultDocument[] = [];
    for (const doc of docs) {
        const claimedDoc = await claimOne(doc, deps);
        if (claimedDoc) {
            claimedDocs.push(claimedDoc);
        }
    }
    if (claimedDocs.length > 0) {
        await processClaimedBatch(claimedDocs, deps);
    }
    return claimedDocs.length;
}

async function claimOne(
    doc: TaggerImageResultDocument,
    deps: TaggerTriggerWorkerDeps
): Promise<TaggerImageResultDocument | null> {
    const path = doc.pixiv_addr;
    const claimedDoc = await deps.repository.claimDueTriggerImage({
        path,
        leaseToken: (deps.leaseToken ?? randomUUID)(),
        processingTimeoutMs: deps.config.processingTimeoutMs,
    });
    if (!claimedDoc) {
        console.log(`Tagger trigger claim skipped: pixiv_addr=${path}`);
        return null;
    }
    return claimedDoc;
}

async function processClaimedBatch(
    docs: TaggerImageResultDocument[],
    deps: TaggerTriggerWorkerDeps
): Promise<void> {
    const result = await triggerTaggerForPixivAddrs(docs.map((doc) => doc.pixiv_addr), {
        syncPixivToMinio: deps.syncPixivToMinio ?? syncPixivToMinioForTagger,
        submitClient: deps.submitClient,
        callbackUrl: deps.config.callbackUrl,
    });

    if (result.status === 'submitted') {
        const claims = result.items.map((item) => processingClaimFor(item.pixivAddr, docs));
        await deps.repository.markSubmitted({ taskId: result.taskId, claims });
        for (const item of result.items) {
            console.log(
                `Tagger trigger submitted: pixiv_addr=${item.pixivAddr} object_name=${item.objectName} task_id=${result.taskId} batch_size=${result.items.length}`
            );
        }
    } else if (result.status === 'submit_failed') {
        console.warn(
            `Tagger trigger submit failed: paths=${result.items.map((item) => item.objectName).join(',')} error=${result.error}`
        );
        for (const item of result.items) {
            const doc = docs.find((candidate) => candidate.pixiv_addr === item.pixivAddr);
            if (doc) {
                await handleSubmitFailure(doc, result.error, result.retryable, deps);
            }
        }
    }

    for (const skipped of result.skipped) {
        const doc = docs.find((item) => item.pixiv_addr === skipped.pixivAddr);
        if (!doc) {
            continue;
        }
        await handleSkipped(doc, skipped, deps);
    }
}

async function handleSubmitFailure(
    doc: TaggerImageResultDocument,
    error: string,
    retryable: boolean,
    deps: TaggerTriggerWorkerDeps
): Promise<void> {
    const attempts = (doc.attempts ?? 0) + 1;
    if (!retryable || attempts >= deps.config.maxAttempts) {
        await deps.repository.markSubmitFailed({ claims: [processingClaim(doc)], error });
        return;
    }

    await deps.repository.markRetry({
        path: doc.pixiv_addr,
        generation: requireGeneration(doc),
        leaseToken: requireProcessingLease(doc),
        error,
        attempts,
        nextAttemptAt: new Date(Date.now() + deps.config.retryDelayMs),
    });
}

async function handleSkipped(
    doc: TaggerImageResultDocument,
    skipped: TriggerSkippedItem,
    deps: TaggerTriggerWorkerDeps
): Promise<void> {
    const path = doc.pixiv_addr;
    const attempts = (doc.attempts ?? 0) + 1;
    const error = formatSkippedError(skipped);
    if (attempts >= deps.config.maxAttempts || skipped.reason === 'disabled') {
        await deps.repository.markSubmitFailed({ claims: [processingClaim(doc)], error });
        console.warn(`Tagger trigger exhausted: pixiv_addr=${path} attempts=${attempts} error=${error}`);
        return;
    }

    const nextAttemptAt = new Date(Date.now() + deps.config.retryDelayMs);
    await deps.repository.markRetry({
        path,
        generation: requireGeneration(doc),
        leaseToken: requireProcessingLease(doc),
        error,
        attempts,
        nextAttemptAt,
    });
    console.warn(
        `Tagger trigger will retry: pixiv_addr=${path} attempts=${attempts}/${deps.config.maxAttempts} next_attempt_at=${nextAttemptAt.toISOString()} error=${error}`
    );
}

function processingClaimFor(
    pixivAddr: string,
    docs: TaggerImageResultDocument[]
): { path: string; generation: number; leaseToken: string } {
    const doc = docs.find((candidate) => candidate.pixiv_addr === pixivAddr);
    if (!doc) throw new Error(`missing claimed tagger image: ${pixivAddr}`);
    return processingClaim(doc);
}

function processingClaim(
    doc: TaggerImageResultDocument
): { path: string; generation: number; leaseToken: string } {
    return {
        path: doc.pixiv_addr,
        generation: requireGeneration(doc),
        leaseToken: requireProcessingLease(doc),
    };
}

function requireGeneration(doc: TaggerImageResultDocument): number {
    if (doc.generation === undefined) {
        throw new Error(`claimed tagger image is missing generation: ${doc.pixiv_addr}`);
    }
    return doc.generation;
}

function requireProcessingLease(doc: TaggerImageResultDocument): string {
    if (!doc.processing_lease_token) {
        throw new Error(`claimed tagger image is missing processing lease: ${doc.pixiv_addr}`);
    }
    return doc.processing_lease_token;
}

function formatSkippedError(result: TriggerSkippedItem): string {
    const details = [
        `reason=${result.reason}`,
        result.objectName ? `object_name=${result.objectName}` : '',
        result.ossKey ? `oss_key=${result.ossKey}` : '',
        result.timeoutMs ? `timeout_ms=${result.timeoutMs}` : '',
        result.error ? `error=${result.error}` : '',
    ].filter(Boolean);
    return details.join(' ');
}
