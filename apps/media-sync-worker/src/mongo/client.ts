import { MongoService, type MongoCollection } from "@inner/shared/mongo";
import { loadPixivMongoConfig } from "./config";
import type { AuthorDownload, DownloadTask, PixivImageInfo, TranslateWord } from "./types";

/**
 * worker 在集群 Mongo（chiwei_pixiv）里的全部集合。
 *
 * 集合名是契约：lark-service 发图读 pixiv_images，tagger projection 写 pixiv_images，
 * 一次性导入按这些名字写入。
 */
export interface PixivCollections {
  /** 图片目录的唯一来源 */
  images: MongoCollection<PixivImageInfo>;
  /** 下载队列 */
  downloadTasks: MongoCollection<DownloadTask>;
  /** tag 翻译表 */
  tagTranslations: MongoCollection<TranslateWord>;
  /** 作者节流：每个作者上次扫描成功的时间 */
  authorDownloads: MongoCollection<AuthorDownload>;
}

export function bindPixivCollections(service: Pick<MongoService, "getCollection">): PixivCollections {
  return {
    images: service.getCollection<PixivImageInfo>("pixiv_images"),
    downloadTasks: service.getCollection<DownloadTask>("pixiv_download_tasks"),
    tagTranslations: service.getCollection<TranslateWord>("pixiv_tag_translations"),
    authorDownloads: service.getCollection<AuthorDownload>("pixiv_author_downloads"),
  };
}

/** 启动时建索引；已存在的同名同定义索引是 no-op。 */
export async function ensurePixivIndexes(collections: PixivCollections): Promise<void> {
  const { images, downloadTasks, tagTranslations } = collections;
  await images.createIndex({ pixiv_addr: 1 }, { background: true, name: "idx_pixiv_images_pixiv_addr" });
  await images.createIndex({ illust_id: -1 }, { background: true, name: "idx_pixiv_images_illust_id_desc" });
  await images.createIndex(
    { update_time: 1, _id: 1 },
    { background: true, name: "idx_pixiv_images_update_time_id" }
  );
  await images.createIndex({ tos_file_name: 1 }, { background: true, name: "idx_pixiv_images_tos_file_name" });
  await images.createIndex(
    { "multi_tags.name": 1 },
    { background: true, name: "idx_pixiv_images_multi_tags_name" }
  );
  await downloadTasks.createIndex(
    { illust_id: 1 },
    { background: true, name: "idx_pixiv_download_tasks_illust_id" }
  );
  // 认领（status 属于 Pending/Fail，或 Running 且 last_run_time 早于回收阈值）和死信清扫
  // （Running、last_run_time 早于阈值、retry_time 耗尽）都是 status 等值在前、
  // last_run_time 范围在后；retry_time 只在 Running 的少量文档里再过滤。
  await downloadTasks.createIndex(
    { status: 1, last_run_time: 1 },
    { background: true, name: "idx_pixiv_download_tasks_status_last_run_time" }
  );
  await tagTranslations.createIndex(
    { origin: 1 },
    { background: true, name: "idx_pixiv_tag_translations_origin" }
  );
}

let collectionsPromise: Promise<PixivCollections> | null = null;

/**
 * 连接集群 Mongo、建索引，返回绑定好的集合。只连一次，失败后下次调用重试。
 *
 * index.ts 在启动任何循环之前先 await 它，连不上就退出；其余调用方随用随取。
 */
export function pixivCollections(): Promise<PixivCollections> {
  if (!collectionsPromise) {
    collectionsPromise = connect().catch((err) => {
      collectionsPromise = null;
      throw err;
    });
  }
  return collectionsPromise;
}

async function connect(): Promise<PixivCollections> {
  const config = loadPixivMongoConfig();
  const service = new MongoService(config);
  await service.initialize();
  const collections = bindPixivCollections(service);
  await ensurePixivIndexes(collections);
  console.log(`Pixiv Mongo ready: host=${config.host} database=${config.database}`);
  return collections;
}
