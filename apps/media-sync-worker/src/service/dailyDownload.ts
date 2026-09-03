import { send_msg } from "../lark";
import { getMaxIllustId, insertDownloadTask } from "../mongo/service";
import {
  getAuthorArtwork,
  getFollowersByTag,
  getTagArtwork,
} from "../pixiv/pixiv";
import { FollowerInfo } from "../pixiv/types";
import redisClient from "../redis/redisClient";
import { ensureDownloadTaskRepositoryReady } from "../mongo/client";
import {
  assertDailyAuthorBatchSucceeded,
  runDailyAuthorBatch,
} from "./dailyAuthorBatch";
import {
  enqueueDownloadTasks,
  runDailyAuthorDiscovery,
  throwIfAborted,
  type DailyAuthorDiscoveryResult,
} from "./dailyAuthorDiscovery";
import {
  loadDailyDownloadGuardConfig,
  loadDownloadDelayConfig,
  waitMs,
} from "./downloadRuntime";
import { sendDailyNotificationWithTimeout } from "./dailyNotification";

const RedisDownloadUserDictKey = "download_user_dict";
const DAILY_NOTIFICATION_TIMEOUT_MS = 30_000;

const sendDailyDownloadNotification = (message: string): Promise<void> =>
  sendDailyNotificationWithTimeout(
    () => send_msg(process.env.SELF_CHAT_ID!, message),
    DAILY_NOTIFICATION_TIMEOUT_MS
  );

const getRandomDays = (): number => {
  return Math.floor(Math.random() * 3) + 2;
};

// 异步下载服务
export const startDownload = async (): Promise<void> => {
  console.log("Download service started...");
  const delayConfig = loadDownloadDelayConfig();
  const guardConfig = loadDailyDownloadGuardConfig();

  try {
    await ensureDownloadTaskRepositoryReady();

    // 获取 "已上传" 标签下的关注者
    const authorArr = await getFollowersByTag("已上传");

    // 如果成功获取关注者
    if (authorArr && authorArr.length > 0) {
      const summary = await runDailyAuthorBatch(authorArr, {
        authorTimeoutMs: guardConfig.authorTimeoutMs,
        runAuthor: (author, signal) =>
          downloadEachUser(author, delayConfig, signal),
        afterAuthor: async (author, result) => {
          if (result === "completed") {
            await redisClient.hset(
              RedisDownloadUserDictKey,
              author.userId,
              `${Math.floor(Date.now() / 1000)}`
            );
          }
        },
      });
      const summaryLog = `daily_download_author_batch ${JSON.stringify(summary)}`;
      if (summary.status === "completed_with_errors") {
        console.warn(summaryLog);
      } else {
        console.info(summaryLog);
      }
      assertDailyAuthorBatchSucceeded(summary);
    } else {
      // 如果没有关注者，发送消息
      await sendDailyDownloadNotification("没有找到关注者");
    }
  } catch (err) {
    // 如果获取关注者出错，发送错误消息
    console.error("Daily download failed:", err);
    try {
      await sendDailyDownloadNotification("下载图片服务执行失败");
    } catch (notifyError) {
      console.error("Failed to notify daily download failure:", notifyError);
    }
    throw err;
  }
};

const downloadEachUser = async (
  author: FollowerInfo,
  delayConfig: ReturnType<typeof loadDownloadDelayConfig>,
  signal: AbortSignal
): Promise<DailyAuthorDiscoveryResult> => {
  console.log(`Downloading images for author: ${author.userName}`);
  const authorId = author.userId;

  try {
    const result = await runDailyAuthorDiscovery(
      author,
      {
        getLastDownloadTime: async (currentAuthorId) => {
          const lastDownloadTime = await redisClient.hget(
            RedisDownloadUserDictKey,
            currentAuthorId
          );
          if (!lastDownloadTime) {
            console.log(
              `Redis field is empty, starting download for author: ${currentAuthorId}`
            );
          }
          return lastDownloadTime;
        },
        discoverAuthor: async (currentAuthorId, currentSignal) => {
          await DownloadIllusts(
            {
              authorId: currentAuthorId,
              authorLastFilter: true,
            },
            currentSignal
          );
        },
        waitAfterAuthor: () => waitMs(delayConfig.afterAuthorMs),
        getRandomDays,
        now: Date.now,
      },
      signal
    );

    if (result === "completed") {
      console.log(`Download successful for author: ${authorId}`);
    } else {
      console.log(
        `Skipping download for author: ${authorId}, within restricted time range`
      );
    }
    return result;
  } catch (err) {
    // 错误处理，如果下载失败则发送消息
    console.error(`Download failed for author: ${authorId}:`, err);
    if (!signal.aborted) {
      try {
        await sendDailyDownloadNotification(`作者：${authorId} 图片下载失败`);
      } catch (notifyError) {
        console.error(`Failed to notify download failure for author: ${authorId}:`, notifyError);
      }
    }
    throw err;
  }
};

interface DownloadIllustsReq {
  authorId?: string;
  keyword?: string;
  page?: number;
  limitIllusts?: string[];
  startIndex?: string;
  endIndex?: string;
  authorLastFilter: boolean;
}

export const DownloadIllusts = async (
  req: DownloadIllustsReq,
  signal: AbortSignal
): Promise<void> => {
  let illustIds: string[] = req.limitIllusts || [];

  try {
    throwIfAborted(signal);

    // 1. 如果传入了 authorId，则获取该作者的作品
    if (req.authorId) {
      illustIds = await getAuthorArtwork(req.authorId);
      throwIfAborted(signal);
      console.log(
        `作者：${req.authorId} 查询到 ${illustIds.length} 张图片`
      );
    }

    // 2. 如果传递了 keyword，则获取与该关键词相关的作品
    if (req.keyword) {
      illustIds = await getTagArtwork(req.keyword, req.page || 1);
      throwIfAborted(signal);
    }

    // 3. 对作品ID进行排序，按降序排列
    illustIds.sort((a, b) => parseInt(b, 10) - parseInt(a, 10));

    if (req.startIndex) {
      const startIndexPos = illustIds.indexOf(req.startIndex);
      if (startIndexPos === -1) {
        throw new Error("startIndex not found");
      }
      illustIds = illustIds.slice(startIndexPos);
    }

    if (req.endIndex) {
      const endIndexPos = illustIds.indexOf(req.endIndex);
      if (endIndexPos === -1) {
        throw new Error("endIndex not found");
      }
      illustIds = illustIds.slice(0, endIndexPos + 1);
    }

    if (illustIds.length === 0) {
      return;
    }

    // 如果需要过滤作者的最后作品
    if (req.authorLastFilter) {
      const maxIllustId = await getMaxIllustId(
        illustIds.map((id) => parseInt(id, 10))
      );
      throwIfAborted(signal);
      if (!maxIllustId) {
        await sendDailyDownloadNotification(
          `作者：${req.authorId} 历史没有数据，请注意`
        );
        throwIfAborted(signal);
      } else {
        console.log(
          `作者：${req.authorId} 历史最大作品ID为 ${maxIllustId}, 过滤掉作者最后作品`
        );
        illustIds = illustIds.slice(
          0,
          illustIds.indexOf(maxIllustId.toString())
        );
      }
    }

    // 从 Redis 获取 ban_illusts 列表
    const banIllusts = await redisClient.smembers("ban_illusts");
    throwIfAborted(signal);

    // 过滤掉被禁止的作品
    if (banIllusts) {
      illustIds = illustIds.filter((id) => !banIllusts.includes(id));
    }

    if (illustIds.length === 0) {
      if (req.authorId) {
        console.log(`作者：${req.authorId}跳过下载`);
      } else if (req.keyword) {
        console.log(`关键词：${req.keyword}跳过下载`);
      }
      return;
    }

    // 记录开始下载日志
    if (req.authorId) {
      console.log(`作者：${req.authorId}开始下载${illustIds.length}张图片`);
      await sendDailyDownloadNotification(
        `作者：${req.authorId} 开始下载 ${illustIds.length} 张图片`
      );
      throwIfAborted(signal);
    } else if (req.keyword) {
      console.log(`关键词：${req.keyword}开始下载${illustIds.length}张图片`);
      await sendDailyDownloadNotification(
        `关键词：${req.keyword} 开始下载 ${illustIds.length} 张图片`
      );
      throwIfAborted(signal);
    }

    await enqueueDownloadTasks(
      illustIds,
      async (illustId) => {
        const insertSuccess = await insertDownloadTask(illustId);
        if (insertSuccess) {
          console.log(`插入任务 ${illustId} 成功`);
        } else {
          console.log(`任务 ${illustId} 已存在`);
        }
        return insertSuccess;
      },
      signal
    );
  } catch (err) {
    console.error("下载图片时发生错误: ", err);
    throw err;
  }
};
