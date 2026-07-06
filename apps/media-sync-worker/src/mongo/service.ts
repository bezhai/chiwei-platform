import {
  Filter,
  MatchKeysAndValues,
  MongoError,
  SortDirection,
  UpdateFilter,
  UpdateOptions,
  UpdateResult,
} from "mongodb";
import { MongoCollection } from "@inner/shared/mongo";
import {
  DownloadTask,
  DownloadTaskStatus,
  MultiTag,
  PixivImageInfo,
  TranslateWord,
    UploadImgV2Req,
} from "./types";
import { DownloadTaskMap, ImgCollection, TranslateWordMap } from "./client";
import { loadConsumerGuardConfig } from "../service/downloadRuntime";

/**
 * 获取给定 illustIds 中最大值的 illust_id
 * @param collection - MongoDB 集合封装类
 * @param illustIds - 要查询的 illust_id 列表
 * @returns 最大的 illust_id 或 0（如果找不到）
 */
export async function getMaxIllustId(illustIds: number[]): Promise<number> {
  try {
    // 构建查询条件，查询 illust_id 在给定数组中的文档
    const filter = {
      illust_id: { $in: illustIds },
    };

    // 设置查询选项：排序 illust_id 倒序，限制结果数量为 1
    const options = {
      limit: 1,
      sort: { illust_id: -1 as SortDirection }, // 按照 illust_id 倒序排列
    };

    const result = await ImgCollection.find(filter, options);

    // 如果查询结果为空，返回 0
    if (result.length === 0) {
      return 0;
    }

    // 返回找到的最大 illust_id
    return result[0].illust_id || 0;
  } catch (error) {
    console.error("Error fetching max illust_id:", error);
    throw error;
  }
}

/**
 * 插入新的下载任务
 * @param illustId - 插画的 ID
 * @returns 返回一个 Promise，表示是否成功插入新任务
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function insertDownloadTask(illustId: string): Promise<boolean> {
  // 查询是否已经存在相同 illustId 的任务
  const filter: Filter<DownloadTask> = { illust_id: illustId };

  // 使用封装的 find 方法查找是否已经存在此任务
  const existingTasks = await DownloadTaskMap.find(filter);

  // 如果找到了现有任务，返回 false 表示未插入
  if (existingTasks.length > 0) {
    return false;
  }

  // 创建新的下载任务
  const newTask = DownloadTask.createTask(illustId);

  // 插入新任务
  await DownloadTaskMap.insertOne(newTask);

  // 返回 true 表示成功插入
  return true;
}

/**
 * 构建「可领取任务」的查询条件。
 *
 * 语义：Pending / Fail 无条件可领取；Running 仅当 last_run_time 早于回收阈值时可领取
 * （consumer 卡死或部署杀 pod 后滞留的任务由此被重新捞回）。Success / Dead 永不匹配。
 *
 * @param now - 当前时间（由调用方传入以保证可单测）
 * @param reclaimMs - Running 任务回收阈值（毫秒）
 */
export function buildClaimableTaskFilter(
  now: Date,
  reclaimMs: number
): Filter<DownloadTask> {
  return {
    $or: [
      { status: { $in: [DownloadTaskStatus.Pending, DownloadTaskStatus.Fail] } },
      {
        status: DownloadTaskStatus.Running,
        last_run_time: { $lt: new Date(now.getTime() - reclaimMs) },
      },
    ],
  };
}

/**
 * 构建「领取任务」的原子更新操作（配合 findOneAndUpdate 使用）。
 *
 * last_run_time 同时充当领取代次：收尾更新以它为条件（见 buildCompletionFilter），
 * 旧轮次迟到的收尾因代次不中而被静默丢弃。retry_time 由 $inc 服务端自增——回收重领
 * 同样消耗重试预算，连续多次 consumer 死亡会把任务推进 Dead 留痕，而不是无限重试。
 *
 * @param now - 领取时间，写入 last_run_time / update_time
 */
export function buildClaimUpdate(now: Date): UpdateFilter<DownloadTask> {
  return {
    $set: {
      status: DownloadTaskStatus.Running,
      last_run_time: now,
      update_time: now,
      last_run_error: "",
    },
    $inc: { retry_time: 1 },
  };
}

/**
 * 构建「回收预算耗尽」的死信清扫条件。
 *
 * 毒任务（每次领取都把 consumer 拖到挂死/超时）永远走不到 fail() 的收尾路径,
 * 只靠领取 $inc 涨 retry_time——不清扫就会每隔回收阈值被无限重领。领取前把
 * 超阈值 Running 且预算耗尽（retry_time >= MaxRetryTime）的任务批量标 Dead 留痕。
 *
 * @param now - 当前时间（由调用方传入以保证可单测）
 * @param reclaimMs - Running 任务回收阈值（毫秒），与领取条件同一阈值
 */
export function buildExhaustedReclaimFilter(
  now: Date,
  reclaimMs: number
): Filter<DownloadTask> {
  return {
    status: DownloadTaskStatus.Running,
    last_run_time: { $lt: new Date(now.getTime() - reclaimMs) },
    retry_time: { $gte: DownloadTask.MaxRetryTime },
  };
}

/**
 * 构建「死信化」更新字段。last_run_error 写明来自回收清扫,
 * 便于运维区分「下载失败进 Dead」和「反复拖死 consumer 进 Dead」。
 *
 * 返回裸字段而非 {$set:...}:MongoCollection.updateMany 会自己包一层 $set,
 * 预包裹会变成 {$set:{$set:...}} 在运行时报错。
 */
export function buildDeadLetterUpdate(now: Date): MatchKeysAndValues<DownloadTask> {
  return {
    status: DownloadTaskStatus.Dead,
    update_time: now,
    last_run_error:
      "dead-lettered by reclaim sweep: retry budget exhausted without completion",
  };
}

/**
 * 构建「收尾更新（Success/Fail）」的查询条件。
 *
 * 除 illust_id 外把 last_run_time 钉在本次领取的代次上：若任务已被回收重领
 * （last_run_time 被改写），旧轮次迟到的收尾条件不中、不会覆盖新领取。
 *
 * @param illustId - 任务 ID
 * @param claimedRunTime - 领取时写入的 last_run_time；undefined 时匹配 null/缺失
 *   （原子领取后必有值，此分支仅为类型完备，永远匹配不到已领取的文档）
 */
export function buildCompletionFilter(
  illustId: string,
  claimedRunTime: Date | undefined
): Filter<DownloadTask> {
  return {
    illust_id: illustId,
    last_run_time: (claimedRunTime ??
      null) as Filter<DownloadTask>["last_run_time"],
  };
}

/**
 * 原子领取一个可执行的下载任务（Pending / Fail / 超过回收阈值的 Running）
 * @returns 返回一个 Promise，表示领取到的任务；如果没有任务返回 null
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function SearchUnDownloadTask(): Promise<DownloadTask | null> {
  const now = new Date();
  const { runningTaskReclaimMs } = loadConsumerGuardConfig();

  // 领取前先死信化清扫预算耗尽的滞留任务，否则它们会被无限回收重领
  const swept = await DownloadTaskMap.updateMany(
    buildExhaustedReclaimFilter(now, runningTaskReclaimMs),
    buildDeadLetterUpdate(now)
  );
  if (swept.modifiedCount > 0) {
    console.warn(
      `Dead-lettered ${swept.modifiedCount} stale Running task(s) with exhausted retry budget`
    );
  }

  // findOneAndUpdate 原子领取，杜绝 find+update 之间被其他协程抢占的窗口
  const claimed = await DownloadTaskMap.findOneAndUpdate(
    buildClaimableTaskFilter(now, runningTaskReclaimMs),
    buildClaimUpdate(now),
    { returnDocument: "after" }
  );

  if (!claimed) {
    return null;
  }

  return new DownloadTask(claimed);
}

/**
 * 更新任务状态为失败，并记录失败原因。
 * 仅当任务仍属于本次领取代次时生效；已被回收重领的任务收尾静默丢弃。
 * @param task - 任务对象
 * @param createErr - 任务失败时的错误信息
 * @returns 返回一个 Promise，表示是否成功更新任务状态
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function Fail(
  task: DownloadTask,
  createErr: Error
): Promise<void> {
  // 先取领取代次构建条件，再生成更新操作
  const filter = buildCompletionFilter(task.illust_id, task.last_run_time);

  // 调用任务对象的 fail 方法，生成更新操作
  const update = task.fail(createErr);

  // 更新数据库中的任务状态
  await DownloadTaskMap.updateOne(filter, update);
}

/**
 * 更新任务状态为成功。
 * 仅当任务仍属于本次领取代次时生效；已被回收重领的任务收尾静默丢弃。
 * @param task - 任务对象
 * @returns 返回一个 Promise，表示是否成功更新任务状态
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function Success(task: DownloadTask): Promise<void> {
  // 先取领取代次构建条件，再生成更新操作
  const filter = buildCompletionFilter(task.illust_id, task.last_run_time);

  // 调用任务对象的 success 方法，生成更新操作
  const update = task.success();

  // 更新数据库中的任务状态
  await DownloadTaskMap.updateOne(filter, update);
}

/**
 * 添加翻译字段并更新数据库
 * @param translateItem - 翻译词条对象
 * @param updateImg - 是否更新图片信息
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function addTranslate(
  translateItem: TranslateWord,
  updateImg: boolean
): Promise<void> {
  try {
    // 构建查询条件
    const filter = { origin: translateItem.origin };

    // 更新翻译字段，若不存在则插入
    const updateOptions: UpdateOptions = { upsert: true };
    await TranslateWordMap.updateMany(filter, translateItem, updateOptions);

    // 如果需要更新图片信息，并且翻译已存在
    if (updateImg && translateItem.has_translate) {
      const imgFilter = { "multi_tags.name": translateItem.origin };
      const imgUpdate = {
        "multi_tags.$.translation": translateItem.translation,
      };

      // 更新图片中的标签翻译
      await ImgCollection.updateMany(imgFilter, imgUpdate);
    }
  } catch (err) {
    console.error(
      `Error updating translation for ${translateItem.origin}:`,
      err
    );
    throw err;
  }
}

/**
 * 查找并添加翻译
 * @param word - 原始词条
 * @param en - 英文翻译
 * @param zh - 中文翻译
 * @returns 返回找到或添加的翻译
 * @throws 如果发生数据库错误或其他错误，将抛出错误
 */
export async function searchAndAddTranslate(
  word: string,
  en: string,
  zh: string
): Promise<string> {
  try {
    // 查找翻译
    const item = await TranslateWordMap.findOne({
      origin: word,
      has_translate: true,
    });

    // 如果找到翻译，返回翻译内容
    if (item && item.translation) {
      return item.translation;
    }

    // 如果没有找到翻译，则添加新的翻译
    await addTranslate(
      {
        origin: word,
        extra_info: {
          zh,
          en,
        },
        has_translate: false, // 因为没有翻译，所以设置为 false
      },
      false
    );

    // 如果没有找到翻译且刚刚添加了翻译，返回空字符串
    return "";
  } catch (err) {
    // 如果错误是 MongoDB 没有找到文档的错误
    if (err instanceof MongoError && err.code === 11000) {
      console.error("No document found for", word);
    }

    // 抛出其他错误
    throw err;
  }
}

/**
 * 检查 Pixiv 图片是否存在
 * @param imgName - 图片名称
 * @returns 如果图片存在则返回 true，否则返回 false
 */
export async function checkExistPixivImg(imgName: string): Promise<boolean> {
  if (imgName === "") {
    return false;
  }

  try {
    // 查询条件
    const filter: Filter<any> = {
      pixiv_addr: imgName,
      tos_file_name: { $ne: "" },
      illust_id: { $ne: 0 },
    };

    // 计数符合条件的文档
    const count = await ImgCollection.countDocuments(filter);
    return count > 0;
  } catch (err) {
    console.error("Failed to count documents:", err);
    return false;
  }
}

/**
 * 构建「取已落 OSS 的图片文档」的 Mongo 查询条件。
 *
 * 语义：按 pixiv_addr 命中，且只匹配 tos_file_name 非空（非 null、非空串）的文档——
 * 历史上同一 pixiv_addr 可能有重复 / 空 tos_file_name 的文档，不加这个约束 findOne
 * 可能挑到空 key 的那条，漏掉真实已写 OSS 的文档（MinIO 同步会拿不到 key 而跳过）。
 *
 * @param pixivAddr - Pixiv 图片名（imageUrl 最后一段）
 * @returns 查询 filter；pixivAddr 为空串时返回 null（调用方据此短路成 null 文档）
 */
export function buildImageByPixivAddrFilter(
  pixivAddr: string
): Filter<PixivImageInfo> | null {
  if (pixivAddr === "") {
    return null;
  }

  return {
    pixiv_addr: pixivAddr,
    // DB 里 tos_file_name 实际可能为 null / 缺失 / 空串（比 TS interface 的 string 宽），
    // $nin [null, ''] 同时排除这三种（$nin 含 null 时也排除字段缺失的文档）。
    tos_file_name: { $nin: [null, ""] } as Filter<PixivImageInfo>["tos_file_name"],
  };
}

/**
 * 按 pixiv_addr 取一条已落 OSS 的图片文档（用于读取代理回填的 tos_file_name）。
 * @param pixivAddr - Pixiv 图片名（imageUrl 最后一段）
 * @returns 命中的文档；未命中（含 pixivAddr 为空 / 无非空 tos_file_name）返回 null
 * @throws 数据库错误向上抛出，由调用方决定是否吞错（best-effort 同步在外层兜）
 */
export async function findImageByPixivAddr(
  pixivAddr: string
): Promise<PixivImageInfo | null> {
  const filter = buildImageByPixivAddrFilter(pixivAddr);
  if (filter === null) {
    return null;
  }

  return ImgCollection.findOne(filter);
}

/**
 * 将字符串的前缀部分转换为整数，如果失败则返回默认值
 * @param pixivAddr - Pixiv 地址
 * @returns 提取到的 illust_id 或默认值 0
 */
export function getIllustId(pixivAddr: string): number {
  // 拆分字符串，取下划线前的部分
  const parts = pixivAddr.split("_");

  // 尝试将第一部分转换为整数
  return intDefault(parts[0], 0);
}

/**
 * 将字符串转换为整数，如果转换失败则返回默认值
 * @param str - 要转换的字符串
 * @param defaultValue - 转换失败时返回的默认值
 * @returns 转换后的整数或默认值
 */
function intDefault(str: string, defaultValue: number): number {
  const parsed = parseInt(str, 10);

  // 如果解析结果为 NaN，则返回默认值
  return isNaN(parsed) ? defaultValue : parsed;
}

/**
 * 添加图片信息到数据库
 * @param tags - 图片的标签数组
 * @param args - 上传图片的请求参数
 * @returns Promise<void> 如果操作成功则返回，否则抛出异常
 */
export async function addImage(
  tags: MultiTag[],
  args: UploadImgV2Req
): Promise<void> {
  // 将标签设置为可见
  tags.forEach((tag) => {
    tag.visible = true;
  });

  try {
    // 更新图片信息至数据库
    await ImgCollection.updateMany(
      { pixiv_addr: args.pixiv_name }, // 查找条件
      {
        multi_tags: tags,
        pixiv_addr: args.pixiv_name,
        visible: !args.is_r18,
        author: args.author,
        create_time: new Date(),
        update_time: new Date(),
        need_download: args.need_download,
        author_id: args.author_id,
        illust_id: getIllustId(args.pixiv_name),
        title: args.title,
        del_flag: false,
      },
      { upsert: true } // 如果没有找到文档，则插入新的文档
    );
    console.log(`Image added successfully for ${args.pixiv_name}`);
  } catch (err) {
    console.error("Error in addImage:", err);
    throw err;
  }
}
