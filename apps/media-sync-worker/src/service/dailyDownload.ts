import { send_msg } from "../lark";
import { getMaxIllustId, insertDownloadTask } from "../mongo/service";
import {
  getAuthorArtwork,
  getFollowersByTag,
  getTagArtwork,
} from "../pixiv/pixiv";
import { FollowerInfo } from "../pixiv/types";
import redisClient from "../redis/redisClient";

const RedisDownloadUserDictKey = "download_user_dict";

const getRandomDays = (): number => {
  return Math.floor(Math.random() * 3) + 2;
};

// 异步下载服务
export const startDownload = async (): Promise<void> => {
  console.log("Download service started...");

  try {
    // 获取 "已上传" 标签下的关注者
    const authorArr = await getFollowersByTag("已上传");

    // 如果成功获取关注者
    if (authorArr && authorArr.length > 0) {
      // console.log('Fetched authors:', authorArr);
      // 这里放置你的下载逻辑
      // 假设你需要对每个作者执行下载操作
      for (const author of authorArr) {
        await downloadEachUser(author);
      }
    } else {
      // 如果没有关注者，发送消息
      await send_msg(process.env.SELF_CHAT_ID!, "没有找到关注者");
    }
  } catch (err) {
    // 如果获取关注者出错，发送错误消息
    console.error("Error fetching followers:", err);
    await send_msg(process.env.SELF_CHAT_ID!, "下载图片服务获取元信息失败");
  }
};

const downloadEachUser = async (author: FollowerInfo): Promise<void> => {
  console.log(`Downloading images for author: ${author.userName}`);
  // 执行下载逻辑...
  const authorId = author.userId;

  try {
    // 从 Redis 获取对应作者的下载时间，使用封装的 hGetValue 函数
    const lastDownloadTime = await redisClient.hget(
      RedisDownloadUserDictKey,
      authorId
    );

    if (!lastDownloadTime) {
      console.log(
        `Redis field is empty, starting download for author: ${authorId}`
      );
    } else {
      const lastDownloadTimestamp = parseInt(lastDownloadTime, 10);
      const randDay = getRandomDays(); // 随机生成 2 到 4 天
      const nextAllowedDownloadTime =
        new Date(lastDownloadTimestamp * 1000).getTime() +
        randDay * 24 * 60 * 60 * 1000;

      // 如果距离上次下载不足指定天数，则跳过该作者
      if (nextAllowedDownloadTime > Date.now()) {
        console.log(
          `Skipping download for author: ${authorId}, within restricted time range`
        );
        return;
      }
    }

    // 更新 Redis 中的下载时间，使用封装的 hSetValue 函数
    await redisClient.hset(
      RedisDownloadUserDictKey,
      authorId,
      `${Math.floor(Date.now() / 1000)}`
    );

    // 执行下载逻辑
    const downloadRequest = {
      authorId: authorId,
      authorLastFilter: true,
    };

    const result = await DownloadIllusts(downloadRequest);

    // 模拟下载延迟
    await new Promise((resolve) => setTimeout(resolve, 3000));

    if (result) {
      console.log(`Download successful for author: ${authorId}`);
    } else {
      throw new Error(`Download failed for author: ${authorId}`);
    }
  } catch (err) {
    // 错误处理，如果下载失败则发送消息
    console.error(`Download failed for author: ${authorId}:`, err);
    await send_msg(process.env.SELF_CHAT_ID!, `作者：${authorId} 图片下载失败`);
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
  req: DownloadIllustsReq
): Promise<boolean> => {
  let illustIds: string[] = req.limitIllusts || [];

  try {
    // 1. 如果传入了 authorId，则获取该作者的作品
    if (req.authorId) {
      illustIds = await getAuthorArtwork(req.authorId);
      console.log(
        `作者：${req.authorId} 查询到 ${illustIds.length} 张图片`
      );
    }

    // 2. 如果传递了 keyword，则获取与该关键词相关的作品
    if (req.keyword) {
      illustIds = await getTagArtwork(req.keyword, req.page || 1);
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
      return true; // 如果没有作品ID，直接返回
    }

    // 如果需要过滤作者的最后作品
    if (req.authorLastFilter) {
      const maxIllustId = await getMaxIllustId(
        illustIds.map((id) => parseInt(id, 10))
      );
      if (!maxIllustId) {
        await send_msg(
          process.env.SELF_CHAT_ID!,
          `作者：${req.authorId} 历史没有数据，请注意`
        );
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
      return true;
    }

    // 记录开始下载日志
    if (req.authorId) {
      console.log(`作者：${req.authorId}开始下载${illustIds.length}张图片`);
      await send_msg(
        process.env.SELF_CHAT_ID!,
        `作者：${req.authorId} 开始下载 ${illustIds.length} 张图片`
      );
    } else if (req.keyword) {
      console.log(`关键词：${req.keyword}开始下载${illustIds.length}张图片`);
      await send_msg(
        process.env.SELF_CHAT_ID!,
        `关键词：${req.keyword} 开始下载 ${illustIds.length} 张图片`
      );
    }

    // 遍历所有作品ID，插入下载任务
    for (const illustId of illustIds) {
      try {
        const insertSuccess = await insertDownloadTask(illustId);
        if (insertSuccess) {
          console.log(`插入任务 ${illustId} 成功`);
        } else {
          console.log(`任务 ${illustId} 已存在`);
        }
      } catch (err) {
        console.error(`插入任务 ${illustId} 失败: ${err}`);
      }
    }
  } catch (err) {
    console.error("下载图片时发生错误: ", err);
    return false;
  }

  return true;
};
