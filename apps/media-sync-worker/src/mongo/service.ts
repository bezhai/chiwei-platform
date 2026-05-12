import {
  Filter,
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
 * 查找一个未下载的任务
 * @returns 返回一个 Promise，表示找到的任务；如果没有任务返回 null
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function SearchUnDownloadTask(): Promise<DownloadTask | null> {
  // 1. 从数据库中查询 status 为 Pending 或 Fail 的任务，限制为 1 个任务
  const filter: Filter<DownloadTask> = {
    status: { $in: [DownloadTaskStatus.Pending, DownloadTaskStatus.Fail] },
  };

  // 使用封装的 find 方法查找未下载的任务
  const existingTasks = await DownloadTaskMap.find(filter, { limit: 1 });

  // 如果没有找到任何任务，返回 null
  if (existingTasks.length === 0) {
    return null;
  }

  const task = new DownloadTask(existingTasks[0]); // 获取第一个任务

  // 2. 更新任务状态为“开始下载”
  const updateFilter: Filter<DownloadTask> = { illust_id: task.illust_id };

  // 执行任务状态更新
  await DownloadTaskMap.updateOne(updateFilter, task.startToDownload());

  // 3. 返回找到的任务
  return task;
}

/**
 * 更新任务状态为失败，并记录失败原因
 * @param task - 任务对象
 * @param createErr - 任务失败时的错误信息
 * @returns 返回一个 Promise，表示是否成功更新任务状态
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function Fail(
  task: DownloadTask,
  createErr: Error
): Promise<void> {
  // 构建查询条件
  const filter: Filter<DownloadTask> = { illust_id: task.illust_id };

  // 调用任务对象的 fail 方法，生成更新操作
  const update = task.fail(createErr);

  // 更新数据库中的任务状态
  await DownloadTaskMap.updateOne(filter, update);
}

/**
 * 更新任务状态为成功
 * @param task - 任务对象
 * @returns 返回一个 Promise，表示是否成功更新任务状态
 * @throws 如果在数据库操作中发生错误，将抛出错误
 */
export async function Success(task: DownloadTask): Promise<void> {
  // 构建查询条件
  const filter: Filter<DownloadTask> = { illust_id: task.illust_id };

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
