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
import { EnumIllustType } from "./types";
import { getIllustInfoWithCache, getIllustPageDetail } from "../pixiv/pixiv";
import redisClient from "../redis/redisClient";
import { MultiTag } from "../mongo/types";
import { getContent } from "../pixiv/pixivProxy";

// 异步消费下载任务的函数
export async function consumeDownloadTaskAsync() {
  console.log("Starting async download task consumer...");

  // 创建下载限制器，限制每 60 次下载，时间窗口为 4 分钟
  const downloadLimiter = new DownloadLimiter(60, 4 * 60 * 1000); // 4分钟 = 4 * 60 * 1000 毫秒

  let sleepTime = 1;
  // 无限循环处理任务
  while (true) {
    try {
      // 1. 获取未下载的任务
      const task = await SearchUnDownloadTask();

      if (!task) {
        sleepTime = sleepTime >= 60 ? sleepTime : sleepTime * 2;
        console.log(
          `No pending tasks found. Waiting for ${sleepTime} seconds...`
        );
        await setTimeout(sleepTime * 1000);
        continue;
      }

      sleepTime = 1;

      // 2. 尝试获取下载许可
      await downloadLimiter.tryDownload(); // 等待限流器允许下载

      try {
        // 3. 下载任务
        await downloadIllust(task.illust_id);
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

      // 6. 每个任务处理完后休眠 5 秒
      await setTimeout(5000);
    } catch (err) {
      console.warn(`Error in consumeDownloadTaskAsync:`, err);
    }
  }
}

/**
 * 下载插画
 * @param illustId - 插画的 ID
 * @throws 如果下载失败，将抛出错误
 */
async function downloadIllust(illustId: string) {
  // 获取插画信息（缓存包装器）
  const illustInfo = await getIllustInfoWithCache(illustId);

  await setTimeout(3000);

  const userId = illustInfo.userId;
  const tags = illustInfo.tags?.tags || [];

  // 如果是 GIF 类型，跳过下载
  if (illustInfo.illustType === EnumIllustType.IllustTypeGif) {
    console.info(`插画 ${illustId} 是 GIF，跳过下载`);
    return;
  }

  // 检查是否是被禁用户
  const bannedUsers = await redisClient.smembers("ban_user");
  if (bannedUsers.includes(userId)) {
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

    const skipWords = await redisClient.smembers("skip_words");
    if (skipWords.some((v: string) => filterTag.includes(v))) {
      console.warn(`插画 ${illustId} 包含敏感词 ${filterTag}`);
      return;
    }
  }

  // 获取插画的页面详情
  let pages;
  try {
    pages = await getIllustPageDetail(illustId);
  } catch (error) {
    throw error;
  }

  // 如果页面数量超过 20，截断为前 20 页
  if (pages.length > 20) {
    console.info(`插画 ${illustId} 页数过多，跳过第 20 页之后的内容`);
    pages = pages.slice(0, 20);
  }

  // 生成 multiTags
  const multiTags: MultiTag[] = [];
  for (const tag of tags) {
    if (tag.tag.includes("00收藏") || tag.tag.includes("0user")) {
      continue;
    }

    const translation = await searchAndAddTranslate(
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

  // 并发控制器（限制并发数为 2）
  const tasks = pages.map((page, index) => async () => {
    const imageUrl = page.urls?.original;
    if (!imageUrl) {
      console.warn(`插画 ${illustId} 第 ${index + 1} 页图片地址为空`);
      return;
    }

    const tempSplitRes = imageUrl.split("/");
    const pixivAddr = tempSplitRes[tempSplitRes.length - 1];

    // 检查图片是否已存在
    if (await checkExistPixivImg(pixivAddr)) {
      console.info(`插画 ${illustId} 第 ${index + 1} 页图片已上传`);
      return;
    }

    // 模拟 Go 中的 2 秒延迟
    await setTimeout(2000);

    console.info(`开始下载插画 ${illustId} 第 ${index + 1} 页`);
    try {
      await getContent(imageUrl); // 下载图片内容
    } catch (downloadError) {
      console.error(
        `下载插画 ${illustId} 第 ${index + 1} 页失败: ${downloadError}`
      );
      return;
    }

    // 上传图片信息至数据库
    try {
      await addImage(multiTags, {
        pixiv_name: pixivAddr,
        need_download: true,
        author: illustInfo.userName,
        author_id: userId,
        is_r18: isR18Illust,
        title: illustInfo.illustTitle,
      });
    } catch (uploadError) {
      console.error(
        `上传插画 ${illustId} 第 ${index + 1} 页失败: ${uploadError}`
      );
    }
  });

  // 使用并发限制执行下载任务
  await limitConcurrency(2, tasks);
}
