import type { MultiTag, UploadImgV2Req } from "../mongo/types";
import { elapsedMs, nowMs } from "./downloadRuntime";
import type { PageDownloadOutcome } from "./pageDownloadResult";

export interface PageDownloadMetrics extends PageDownloadOutcome {
  proxyDownloadMs: number;
  addImageMs: number;
  postSyncMs: number;
}

export interface PageDownloadRequest {
  illustId: string;
  /** 从 0 开始，只用于日志 */
  pageIndex: number;
  imageUrl: string | undefined;
  tags: MultiTag[];
  /** 这一页之外、整个作品共用的元信息 */
  image: Omit<UploadImgV2Req, "pixiv_name" | "tos_file_name">;
}

export interface PageDownloadDeps {
  checkExistPixivImg: (pixivAddr: string) => Promise<boolean>;
  waitBeforeDownload: () => Promise<void>;
  /** 让 server 下载并上传 OSS，返回实际写入的 key */
  downloadToOss: (imageUrl: string) => Promise<string>;
  addImage: (tags: MultiTag[], image: UploadImgV2Req) => Promise<void>;
  runPostDownloadSync: (pixivAddr: string) => Promise<unknown>;
}

/**
 * 下载一页：目录里已有完整记录的页只重放后处理；否则让 server 下载上传，用它返回的
 * OSS key 写目录记录，再交给后处理（MinIO 同步 / tagger 入队）。
 *
 * 目录记录必须先于后处理写好：两者都按 pixiv_addr 读这条记录。
 * 不抛错，失败落在返回的 status 上，由调用方等所有页结束后统一判定。
 */
export async function downloadPage(
  request: PageDownloadRequest,
  deps: PageDownloadDeps
): Promise<PageDownloadMetrics> {
  const metrics: PageDownloadMetrics = {
    status: "downloaded",
    proxyDownloadMs: 0,
    addImageMs: 0,
    postSyncMs: 0,
  };
  const { illustId, imageUrl } = request;
  const pageLabel = `插画 ${illustId} 第 ${request.pageIndex + 1} 页`;

  if (!imageUrl) {
    metrics.status = "missing_url";
    console.warn(`${pageLabel}图片地址为空`);
    return metrics;
  }

  const pathSegments = imageUrl.split("/");
  const pixivAddr = pathSegments[pathSegments.length - 1];

  if (await deps.checkExistPixivImg(pixivAddr)) {
    const postSyncStartedAt = nowMs();
    try {
      await deps.runPostDownloadSync(pixivAddr);
      metrics.postSyncMs = elapsedMs(postSyncStartedAt);
      metrics.status = "exists";
      console.info(`${pageLabel}图片已上传，后处理已重放`);
    } catch (postSyncError) {
      metrics.postSyncMs = elapsedMs(postSyncStartedAt);
      metrics.status = "post_sync_failed";
      metrics.error = formatError(postSyncError);
      console.error(`重放${pageLabel}后处理失败: ${postSyncError}`);
    }
    return metrics;
  }

  await deps.waitBeforeDownload();

  console.info(`开始下载${pageLabel}`);
  let tosFileName: string;
  try {
    const downloadStartedAt = nowMs();
    tosFileName = await deps.downloadToOss(imageUrl);
    metrics.proxyDownloadMs = elapsedMs(downloadStartedAt);
  } catch (downloadError) {
    metrics.status = "download_failed";
    metrics.error = formatError(downloadError);
    console.error(`下载${pageLabel}失败: ${downloadError}`);
    return metrics;
  }

  try {
    const addImageStartedAt = nowMs();
    await deps.addImage(request.tags, {
      pixiv_name: pixivAddr,
      tos_file_name: tosFileName,
      ...request.image,
    });
    metrics.addImageMs = elapsedMs(addImageStartedAt);
  } catch (uploadError) {
    metrics.status = "add_image_failed";
    metrics.error = formatError(uploadError);
    console.error(`写入${pageLabel}目录记录失败: ${uploadError}`);
    return metrics;
  }

  const postSyncStartedAt = nowMs();
  try {
    await deps.runPostDownloadSync(pixivAddr);
    metrics.postSyncMs = elapsedMs(postSyncStartedAt);
  } catch (postSyncError) {
    metrics.postSyncMs = elapsedMs(postSyncStartedAt);
    metrics.status = "post_sync_failed";
    metrics.error = formatError(postSyncError);
    console.error(`${pageLabel}后处理失败: ${postSyncError}`);
  }
  return metrics;
}

function formatError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
