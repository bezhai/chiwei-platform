import type { PixivImageMirrorResult } from '../mongo/imageMirror';

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
}

export async function runPostDownloadSync(
    pixivAddr: string,
    deps: PostDownloadSyncDeps = {}
): Promise<DownloadPostSyncResult> {
    const syncPixivImageToLocal = deps.syncPixivImageToLocal ?? (await import('../mongo/imageMirror')).syncPixivImageToLocal;
    const syncMinioAndMaybeSubmitTagger =
        deps.syncMinioAndMaybeSubmitTagger ?? (await import('../tagger/runtime')).syncMinioAndMaybeSubmitTagger;

    const mirrorResult = await syncPixivImageToLocal(pixivAddr);
    if (mirrorResult.status === 'missing_source') {
        throw new Error(`post-download sync missing source image: ${pixivAddr}`);
    }

    const handoffResult = await syncMinioAndMaybeSubmitTagger(pixivAddr);
    if (handoffResult.status === 'enqueue_failed') {
        throw new Error(`post-download durable handoff failed for ${pixivAddr}: ${handoffResult.error}`);
    }

    return {
        ...handoffResult,
        pixiv_image_mirror_status: mirrorResult.status,
        ...('count' in mirrorResult ? { pixiv_image_mirror_count: mirrorResult.count } : {}),
    };
}
