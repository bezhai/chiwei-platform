import { setTimeout } from "timers/promises";
import { DownloadLimiter, limitConcurrency } from "../utils/downloadLimiter";
import {
  addImage,
  checkExistPixivImg,
  Fail,
  searchAndAddTranslate,
  SearchUnDownloadTask,
  Success,
} from "../mongo/service";
import type { IllustrationPageDetail } from "@inner/pixiv-client";
import { EnumIllustType, type GetIllustInfoBody } from "./types";
import { downloadToOss, getIllustInfoWithCache, getIllustPageDetail } from "../pixiv/pixiv";
import { getDownloadFilters, type DownloadFilters } from "../pixiv/settings";
import { DownloadTask, MultiTag } from "../mongo/types";
import {
  elapsedMs,
  loadConsumerGuardConfig,
  loadDownloadDelayConfig,
  nowMs,
  waitMs,
  type DownloadDelayConfig,
} from "./downloadRuntime";
import {
  ConsecutiveTimeoutGuard,
  CycleTimeoutError,
  runWithTimeout,
} from "./consumerWatchdog";
import { runPostDownloadSync } from "./postDownloadSync";
import { assertPageDownloadsSucceeded } from "./pageDownloadResult";
import { downloadPage, type PageDownloadDeps } from "./pageDownload";

type DownloadIllustStatus =
  | "completed"
  | "skipped_gif"
  | "skipped_banned_user"
  | "skipped_sensitive_word"
  | "failed";

interface DownloadIllustTiming {
  illust_id: string;
  status: DownloadIllustStatus;
  page_count: number;
  downloaded_page_count: number;
  skipped_page_count: number;
  failed_page_count: number;
  illust_info_ms: number;
  page_detail_ms: number;
  translate_ms: number;
  proxy_download_ms: number;
  add_image_ms: number;
  post_sync_ms: number;
  total_ms: number;
  error?: string;
}

export type ClaimOutcome =
  | { status: "claimed"; task: DownloadTask; filters: DownloadFilters }
  | { status: "idle" }
  | { status: "filters_unavailable"; error: string };

interface ClaimDeps {
  readFilters: () => Promise<DownloadFilters>;
  claim: () => Promise<DownloadTask | null>;
}

/**
 * 先读名单再领取任务。
 *
 * 领取会消耗任务的重试预算（retry_time +1）。如果先领取、后读名单，Dynamic Config
 * 一出故障，consumer 会把碰到的每个任务都标失败，几分钟内整个队列都会进 Dead。
 * 所以名单读不到时不领取，由调用方退避后再试。
 */
export async function claimNextTask(
  deps: ClaimDeps = { readFilters: getDownloadFilters, claim: () => SearchUnDownloadTask() }
): Promise<ClaimOutcome> {
  let filters: DownloadFilters;
  try {
    filters = await deps.readFilters();
  } catch (err) {
    return { status: "filters_unavailable", error: err instanceof Error ? err.message : String(err) };
  }

  const task = await deps.claim();
  if (!task) {
    return { status: "idle" };
  }
  return { status: "claimed", task, filters };
}

// 连续超时退出阈值：约 3 × 循环上限（默认 3 小时）不消费即判定系统性故障。
// 计数只统计"连续"——空轮次/普通错误轮次会清零，所以单个毒任务（每次领取都拖到
// 超时、但轮次间隔着正常任务）不靠这里终结：它每次领取消耗重试预算，最多
// MaxRetryTime 次后被 SearchUnDownloadTask 的死信化清扫标 Dead。
const CONSECUTIVE_CYCLE_TIMEOUT_EXIT_THRESHOLD = 3;

// 异步消费下载任务的函数
export async function consumeDownloadTaskAsync() {
  console.log("Starting async download task consumer...");

  const delayConfig = loadDownloadDelayConfig();
  console.log(`Download delay config: ${JSON.stringify(delayConfig)}`);

  const guardConfig = loadConsumerGuardConfig();
  console.log(`Consumer guard config: ${JSON.stringify(guardConfig)}`);

  // 创建下载限制器，限制每 60 次下载后进入可配置冷却期（默认 2 分钟）
  const downloadLimiter = new DownloadLimiter(60, delayConfig.limiterCooldownMs);

  const timeoutGuard = new ConsecutiveTimeoutGuard(
    CONSECUTIVE_CYCLE_TIMEOUT_EXIT_THRESHOLD,
    () => {
      console.error(
        `Consumer hit ${CONSECUTIVE_CYCLE_TIMEOUT_EXIT_THRESHOLD} consecutive cycle timeouts; ` +
          `exiting so K8s restarts the pod.`
      );
      process.exit(1);
    }
  );

  let sleepTime = 1;

  // 单轮循环体（被 watchdog 包裹；watchdog 放弃后本轮仍在后台继续，
  // 其迟到的 Success/Fail 收尾由领取代次条件化静默丢弃）
  const runOneCycle = async () => {
    // 1. 读名单并领取一个任务
    const next = await claimNextTask();

    if (next.status !== "claimed") {
      sleepTime = sleepTime >= 60 ? sleepTime : sleepTime * 2;
      if (next.status === "filters_unavailable") {
        console.error(
          `Download filters unavailable, not claiming tasks: ${next.error}. Waiting for ${sleepTime} seconds...`
        );
      } else {
        console.log(
          `No pending tasks found. Waiting for ${sleepTime} seconds...`
        );
      }
      await setTimeout(sleepTime * 1000);
      return;
    }

    sleepTime = 1;
    const { task, filters } = next;

    // 2. 尝试获取下载许可
    await downloadLimiter.tryDownload(); // 等待限流器允许下载

    try {
      // 3. 下载任务
      await downloadIllust(task.illust_id, filters, delayConfig);
      console.log(`Download successful for task: ${task.illust_id}`);

      // 4. 标记任务成功
      await Success(task);
      console.log(`Task ${task.illust_id} marked as success.`);
    } catch (downloadError) {
      console.warn(
        `Download failed for task ${task.illust_id}, error: `,
        downloadError
      );

      // 5. 标记任务失败
      await Fail(task, downloadError as Error);
      console.warn(`Task ${task.illust_id} marked as failed.`);
    }

    // 6. 每个任务处理完后按配置休眠（默认已从旧值 5s 降到 2.5s）
    await waitMs(delayConfig.afterTaskMs);
  };

  // 无限循环处理任务
  while (true) {
    try {
      await runWithTimeout(runOneCycle(), guardConfig.cycleTimeoutMs);
      timeoutGuard.recordSettled();
    } catch (err) {
      if (err instanceof CycleTimeoutError) {
        console.error(`Consumer cycle watchdog fired:`, err);
        timeoutGuard.recordTimeout();
      } else {
        // 普通错误说明本轮已 settle，不属于连续超时
        timeoutGuard.recordSettled();
        console.warn(`Error in consumeDownloadTaskAsync:`, err);
      }
    }
  }
}

/** 下载一个作品用到的外部调用；默认是生产实现，测试注入替身。 */
export interface IllustDownloadDeps extends Omit<PageDownloadDeps, "waitBeforeDownload"> {
  getIllustInfo: (illustId: string) => Promise<GetIllustInfoBody>;
  getIllustPageDetail: (illustId: string) => Promise<IllustrationPageDetail[]>;
  searchAndAddTranslate: (word: string, en: string, zh: string) => Promise<string>;
}

const defaultIllustDownloadDeps: IllustDownloadDeps = {
  getIllustInfo: getIllustInfoWithCache,
  getIllustPageDetail,
  searchAndAddTranslate,
  checkExistPixivImg,
  downloadToOss,
  addImage,
  runPostDownloadSync,
};

/**
 * 下载插画
 * @param illustId - 插画的 ID
 * @param filters - 领取任务前读到的名单
 * @throws 如果下载失败，将抛出错误
 */
export async function downloadIllust(
  illustId: string,
  filters: DownloadFilters,
  delayConfig: DownloadDelayConfig = loadDownloadDelayConfig(),
  deps: IllustDownloadDeps = defaultIllustDownloadDeps
) {
  const totalStartedAt = nowMs();
  const timing: DownloadIllustTiming = {
    illust_id: illustId,
    status: "completed",
    page_count: 0,
    downloaded_page_count: 0,
    skipped_page_count: 0,
    failed_page_count: 0,
    illust_info_ms: 0,
    page_detail_ms: 0,
    translate_ms: 0,
    proxy_download_ms: 0,
    add_image_ms: 0,
    post_sync_ms: 0,
    total_ms: 0,
  };

  // 获取插画信息（缓存包装器）
  try {
    const illustInfoStartedAt = nowMs();
    const illustInfo = await deps.getIllustInfo(illustId);
    timing.illust_info_ms = elapsedMs(illustInfoStartedAt);

    await waitMs(delayConfig.afterIllustInfoMs);

    const userId = illustInfo.userId;
    const tags = illustInfo.tags?.tags || [];

    // 如果是 GIF 类型，跳过下载
    if (illustInfo.illustType === EnumIllustType.IllustTypeGif) {
      timing.status = "skipped_gif";
      console.info(`插画 ${illustId} 是 GIF，跳过下载`);
      return;
    }

    // 检查是否是被禁用户
    if (filters.bannedUsers.includes(userId)) {
      timing.status = "skipped_banned_user";
      console.warn(`用户 ${userId} 已被封禁`);
      return;
    }

    // 检查是否是 R18 插画
    let isR18Illust = false;
    for (const tag of tags) {
      const filterTag =
        (tag.translation?.en ?? "") + (tag.translation?.zh ?? "") + tag.tag;
      if (filterTag.includes("R-18")) {
        isR18Illust = true;
      }

      if (filters.skipWords.some((v: string) => filterTag.includes(v))) {
        timing.status = "skipped_sensitive_word";
        console.warn(`插画 ${illustId} 包含敏感词 ${filterTag}`);
        return;
      }
    }

    // 获取插画的页面详情
    let pages;
    try {
      const pageDetailStartedAt = nowMs();
      pages = await deps.getIllustPageDetail(illustId);
      timing.page_detail_ms = elapsedMs(pageDetailStartedAt);
    } catch (error) {
      throw error;
    }

    // 如果页面数量超过 20，截断为前 20 页
    if (pages.length > 20) {
      console.info(`插画 ${illustId} 页数过多，跳过第 20 页之后的内容`);
      pages = pages.slice(0, 20);
    }
    timing.page_count = pages.length;

    // 生成 multiTags
    const multiTags: MultiTag[] = [];
    const translateStartedAt = nowMs();
    for (const tag of tags) {
      if (tag.tag.includes("00收藏") || tag.tag.includes("0user")) {
        continue;
      }

      const translation = await deps.searchAndAddTranslate(
        tag.tag,
        tag.translation?.en ?? "",
        tag.translation?.zh ?? ""
      );
      multiTags.push({
        name: tag.tag,
        translation,
        visible: true,
      });
    }
    timing.translate_ms = elapsedMs(translateStartedAt);

    // 并发控制器（限制并发数为 2）
    const image = {
      need_download: true,
      author: illustInfo.userName,
      author_id: userId,
      is_r18: isR18Illust,
      title: illustInfo.illustTitle,
    };
    const tasks = pages.map((page, pageIndex) => () =>
      downloadPage(
        { illustId, pageIndex, imageUrl: page.urls?.original, tags: multiTags, image },
        {
          ...deps,
          waitBeforeDownload: () => waitMs(delayConfig.beforePageDownloadMs),
        }
      )
    );

    // 使用并发限制执行下载任务
    const pageMetrics = await limitConcurrency(2, tasks);
    for (const metrics of pageMetrics) {
      timing.proxy_download_ms += metrics.proxyDownloadMs;
      timing.add_image_ms += metrics.addImageMs;
      timing.post_sync_ms += metrics.postSyncMs;
      switch (metrics.status) {
        case "downloaded":
          timing.downloaded_page_count++;
          break;
        case "exists":
          timing.skipped_page_count++;
          break;
        case "missing_url":
        case "download_failed":
        case "add_image_failed":
        case "post_sync_failed":
          timing.failed_page_count++;
          break;
      }
    }
    assertPageDownloadsSucceeded(illustId, pageMetrics);
  } catch (err) {
    timing.status = "failed";
    timing.error = formatError(err);
    throw err;
  } finally {
    timing.total_ms = elapsedMs(totalStartedAt);
    console.info(`download_illust_timing ${JSON.stringify(timing)}`);
  }
}

function formatError(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
