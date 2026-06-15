import { elapsedMs, nowMs as defaultNowMs } from './downloadRuntime';

type PostDownloadSyncResult =
    | { status: 'queued' }
    | { status: 'enqueue_failed'; error: string }
    | { status: 'tagger_disabled' };

interface PostDownloadSyncDeps {
    syncMinioAndMaybeSubmitTagger?: (pixivAddr: string) => Promise<PostDownloadSyncResult>;
    logger?: Pick<Console, 'info' | 'warn'>;
    nowMs?: () => number;
}

export function schedulePostDownloadSync(
    pixivAddr: string,
    deps: PostDownloadSyncDeps = {}
): void {
    const syncFn = deps.syncMinioAndMaybeSubmitTagger ?? defaultPostDownloadSync;
    const logger = deps.logger ?? console;
    const getNow = deps.nowMs ?? defaultNowMs;
    const startedAt = getNow();

    void syncFn(pixivAddr)
        .then((result) => {
            const payload: Record<string, unknown> = {
                pixiv_addr: pixivAddr,
                status: result.status,
                total_ms: elapsedMs(startedAt, getNow),
            };
            if ('error' in result) {
                payload.error = result.error;
            }
            logger.info(`download_post_sync_timing ${JSON.stringify(payload)}`);
        })
        .catch((err) => {
            logger.warn(`download_post_sync_failed pixiv_addr=${pixivAddr}`, err);
        });
}

async function defaultPostDownloadSync(pixivAddr: string): Promise<PostDownloadSyncResult> {
    const { syncMinioAndMaybeSubmitTagger } = await import('../tagger/runtime');
    return syncMinioAndMaybeSubmitTagger(pixivAddr);
}
