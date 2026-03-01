import { Collection } from "mongodb";
import { MongoService, MongoCollection, getMongoService } from "@inner/shared/mongo";
import { DownloadTask, PixivImageInfo, TranslateWord } from "./types";

// MongoDB 服务实例
let mongoService: MongoService;

// 定义 MongoDB 集合实例
export let ImgCollection: MongoCollection<PixivImageInfo>;
export let DownloadTaskMap: MongoCollection<DownloadTask>;
export let TranslateWordMap: MongoCollection<TranslateWord>;

// Bangumi Archive 集合实例（使用原生 Collection）
export let BangumiSubjectCollection: Collection;
export let BangumiCharacterCollection: Collection;
export let BangumiPersonCollection: Collection;
export let BangumiEpisodeCollection: Collection;
export let BangumiSubjectCharacterCollection: Collection;
export let BangumiSubjectPersonCollection: Collection;
export let BangumiPersonCharacterCollection: Collection;
export let BangumiSubjectRelationCollection: Collection;

/**
 * 创建 Bangumi Archive 相关的索引
 */
async function createBangumiIndexes(): Promise<void> {
  console.log('开始创建 Bangumi Archive 索引...');

  try {
    // 主实体表索引（单字段唯一索引）
    await BangumiSubjectCollection.createIndex({ "id": 1 }, { unique: true, background: true, name: "idx_id_unique" });
    await BangumiCharacterCollection.createIndex({ "id": 1 }, { unique: true, background: true, name: "idx_id_unique" });
    await BangumiPersonCollection.createIndex({ "id": 1 }, { unique: true, background: true, name: "idx_id_unique" });
    await BangumiEpisodeCollection.createIndex({ "id": 1 }, { unique: true, background: true, name: "idx_id_unique" });

    // 关系表索引（复合唯一索引）
    await BangumiSubjectCharacterCollection.createIndex(
      { "subject_id": 1, "character_id": 1 },
      { unique: true, background: true, name: "idx_subject_id_character_id_unique" }
    );
    await BangumiSubjectPersonCollection.createIndex(
      { "subject_id": 1, "person_id": 1 },
      { unique: true, background: true, name: "idx_subject_id_person_id_unique" }
    );
    await BangumiPersonCharacterCollection.createIndex(
      { "person_id": 1, "character_id": 1 },
      { unique: true, background: true, name: "idx_person_id_character_id_unique" }
    );
    await BangumiSubjectRelationCollection.createIndex(
      { "subject_id": 1, "relation_subject_id": 1 },
      { unique: true, background: true, name: "idx_subject_id_relation_subject_id_unique" }
    );

    console.log('Bangumi Archive 索引创建完成');
  } catch (error: any) {
    // 索引已存在或其他非致命错误，记录但不中断初始化
    if (error.code !== 85) { // 85 = IndexOptionsConflict
      console.warn('创建 Bangumi Archive 索引时警告:', error.message);
    } else {
      console.log('Bangumi Archive 索引已存在，跳过创建');
    }
  }
}

export const mongoInitPromise = (async () => {
  try {
    // 使用共享的 MongoService
    mongoService = getMongoService({
      host: process.env.MONGO_HOST || 'mongo',
      username: process.env.MONGO_INITDB_ROOT_USERNAME,
      password: process.env.MONGO_INITDB_ROOT_PASSWORD,
      database: 'chiwei',
      authSource: 'admin',
      connectTimeoutMS: 2000,
    });

    await mongoService.initialize();

    // 初始化各个集合
    ImgCollection = mongoService.getCollection<PixivImageInfo>("img_map");
    DownloadTaskMap = mongoService.getCollection<DownloadTask>("download_task");
    TranslateWordMap = mongoService.getCollection<TranslateWord>("trans_map");

    // 初始化 Bangumi Archive 集合
    BangumiSubjectCollection = mongoService.getNativeCollection("bangumi_archive_subjects");
    BangumiCharacterCollection = mongoService.getNativeCollection("bangumi_archive_characters");
    BangumiPersonCollection = mongoService.getNativeCollection("bangumi_archive_persons");
    BangumiEpisodeCollection = mongoService.getNativeCollection("bangumi_archive_episodes");
    BangumiSubjectCharacterCollection = mongoService.getNativeCollection("bangumi_archive_subject_characters");
    BangumiSubjectPersonCollection = mongoService.getNativeCollection("bangumi_archive_subject_persons");
    BangumiPersonCharacterCollection = mongoService.getNativeCollection("bangumi_archive_person_characters");
    BangumiSubjectRelationCollection = mongoService.getNativeCollection("bangumi_archive_subject_relations");

    // 创建 Bangumi Archive 索引
    // await createBangumiIndexes();

    console.log("MongoDB initialization completed.");
  } catch (err) {
    console.error("MongoDB initialization failed:", err);
    throw err;
  }
})();
