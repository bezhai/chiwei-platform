import type { PixivImageMirrorResult } from '../mongo/imageMirror';
import { elapsedMs, nowMs as defaultNowMs } from './downloadRuntime';

type PostDownloadSyncResult =
    | { status: 'queued' }
    | { status: 'enqueue_failed'; error: string }
    | { status: 'tagger_disabled' };

type DownloadPostSyncResult = PostDownloadSyncResult & {
    pixiv_image_mirror_status?: string;
    pixiv_image_mirror_count?: number;
    pixiv_image_mirror_error?: string;
};

interface PostDownloadSyncDeps {
    syncMinioAndMaybeSubmitTagger?: (pixivAddr: string) => Promise<PostDownloadSyncResult>;
    syncPixivImageToLocal?: (pixivAddr: string) => Promise<PixivImageMirrorResult>;
    logger?: Pick<Console, 'info' | 'warn'>;
    nowMs?: () => number;
}

export function schedulePostDownloadSync(
    pixivAddr: string,
    deps: PostDownloadSyncDeps = {}
): void {
    const syncFn =
        deps.syncMinioAndMaybeSubmitTagger && !deps.syncPixivImageToLocal
            ? deps.syncMinioAndMaybeSubmitTagger
            : (addr: string) => runPostDownloadSync(addr, deps);
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
            for (const [key, value] of Object.entries(result)) {
                if (key !== 'status' && key !== 'error') {
                    payload[key] = value;
                }
            }
            logger.info(`download_post_sync_timing ${JSON.stringify(payload)}`);
        })
        .catch((err) => {
            logger.warn(`download_post_sync_failed pixiv_addr=${pixivAddr}`, err);
        });
}

export async function runPostDownloadSync(
    pixivAddr: string,
    deps: PostDownloadSyncDeps = {}
): Promise<DownloadPostSyncResult> {
    const logger = deps.logger ?? console;
    const syncPixivImageToLocal = deps.syncPixivImageToLocal ?? (await import('../mongo/imageMirror')).syncPixivImageToLocal;
    const syncMinioAndMaybeSubmitTagger =
        deps.syncMinioAndMaybeSubmitTagger ?? (await import('../tagger/runtime')).syncMinioAndMaybeSubmitTagger;

    const [mirrorSettled, taggerSettled] = await Promise.allSettled([
        syncPixivImageToLocal(pixivAddr),
        syncMinioAndMaybeSubmitTagger(pixivAddr),
    ]);

    if (taggerSettled.status === 'rejected') {
        throw taggerSettled.reason;
    }

    if (mirrorSettled.status === 'fulfilled') {
        const mirrorResult = mirrorSettled.value;
        return {
            ...taggerSettled.value,
            pixiv_image_mirror_status: mirrorResult.status,
            ...('count' in mirrorResult ? { pixiv_image_mirror_count: mirrorResult.count } : {}),
        };
    }

    const error = mirrorSettled.reason instanceof Error ? mirrorSettled.reason.message : String(mirrorSettled.reason);
    logger.warn(`pixiv_image_mirror_sync_failed pixiv_addr=${pixivAddr} error=${error}`);
    return {
        ...taggerSettled.value,
        pixiv_image_mirror_status: 'failed',
        pixiv_image_mirror_error: error,
    };
}
