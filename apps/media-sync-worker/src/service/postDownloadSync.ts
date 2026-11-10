type PostDownloadSyncResult =
    | { status: 'queued' }
    | { status: 'enqueue_failed'; error: string }
    | { status: 'tagger_disabled' };

interface PostDownloadSyncDeps {
    syncMinioAndMaybeSubmitTagger?: (pixivAddr: string) => Promise<PostDownloadSyncResult>;
}

/**
 * 下载后的后处理：tagger 开启时写入 durable outbox（由 trigger worker 同步 MinIO 并提交），
 * 关闭时直接同步 MinIO。依赖 pixiv_images 里已经有这一页的目录记录。
 */
export async function runPostDownloadSync(
    pixivAddr: string,
    deps: PostDownloadSyncDeps = {}
): Promise<PostDownloadSyncResult> {
    const syncMinioAndMaybeSubmitTagger =
        deps.syncMinioAndMaybeSubmitTagger ?? (await import('../tagger/runtime')).syncMinioAndMaybeSubmitTagger;

    const handoffResult = await syncMinioAndMaybeSubmitTagger(pixivAddr);
    if (handoffResult.status === 'enqueue_failed') {
        throw new Error(`post-download durable handoff failed for ${pixivAddr}: ${handoffResult.error}`);
    }
    return handoffResult;
}
