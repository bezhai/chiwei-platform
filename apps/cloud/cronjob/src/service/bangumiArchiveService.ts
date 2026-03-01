import axios from 'axios';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';
import * as unzipper from 'unzipper';
import { createReadStream } from 'fs';
import { createInterface } from 'readline';
import { 
  mongoInitPromise,
  BangumiSubjectCollection,
  BangumiCharacterCollection,
  BangumiPersonCollection,
  BangumiEpisodeCollection,
  BangumiSubjectCharacterCollection,
  BangumiSubjectPersonCollection,
  BangumiPersonCharacterCollection,
  BangumiSubjectRelationCollection
} from '../mongo/client';
import { Collection } from 'mongodb';

// Bangumi Archive 数据处理服务
export class BangumiArchiveService {
  private readonly LATEST_JSON_URL = 'https://raw.githubusercontent.com/bangumi/Archive/master/aux/latest.json';
  private readonly TEMP_DIR_PREFIX = 'bangumi-archive-';

  /**
   * 主要的数据同步流程
   */
  async syncBangumiArchiveData(): Promise<void> {
    console.log('开始执行 Bangumi Archive 数据同步任务...');
    
    let tempDir: string | null = null;
    
    try {
      // 1. 创建临时目录
      tempDir = await this.createTempDirectory();
      console.log(`临时目录已创建: ${tempDir}`);

      // 2. 获取下载链接
      const downloadUrl = await this.getDownloadUrl();
      if (!downloadUrl) {
        console.log('未找到可用的下载链接，跳过本次同步');
        return;
      }
      console.log(`获取到下载链接: ${downloadUrl}`);

      // 3. 下载 ZIP 文件
      const zipFilePath = await this.downloadZipFile(downloadUrl, tempDir);
      console.log(`ZIP 文件已下载: ${zipFilePath}`);

      // 4. 解压 ZIP 文件
      const extractedDir = await this.extractZipFile(zipFilePath, tempDir);
      console.log(`ZIP 文件已解压到: ${extractedDir}`);

      // 5. 处理 jsonlines 文件
      await this.processJsonlinesFiles(extractedDir);
      console.log('所有 jsonlines 文件处理完成');

    } catch (error) {
      console.error('Bangumi Archive 数据同步过程中发生错误:', error);
      throw error;
    } finally {
      // 6. 清理临时文件
      if (tempDir) {
        await this.cleanupTempDirectory(tempDir);
        console.log('临时文件已清理');
      }
    }
  }

  /**
   * 创建临时目录
   */
  private async createTempDirectory(): Promise<string> {
    const tempDir = fs.mkdtempSync(path.join(os.tmpdir(), this.TEMP_DIR_PREFIX));
    return tempDir;
  }

  /**
   * 从 raw latest.json 获取下载链接
   */
  private async getDownloadUrl(): Promise<string | null> {
    try {
      const response = await axios.get(this.LATEST_JSON_URL, {
        headers: {
          'User-Agent': 'chiwei-bot-cronjob/1.0.0'
        },
        timeout: 10000
      });

      const url: unknown = (response as any)?.data?.browser_download_url;
      if (typeof url === 'string' && url.length > 0) {
        return url;
      }
      console.warn('latest.json 中未找到 browser_download_url');
      return null;
    } catch (error) {
      console.error('获取下载链接失败:', error);
      throw new Error(`获取 latest.json 失败: ${error}`);
    }
  }

  /**
   * 下载 ZIP 文件
   */
  private async downloadZipFile(downloadUrl: string, tempDir: string): Promise<string> {
    const zipFileName = path.basename(new URL(downloadUrl).pathname) || 'bangumi-archive.zip';
    const zipFilePath = path.join(tempDir, zipFileName);

    try {
      const response = await axios({
        method: 'get',
        url: downloadUrl,
        responseType: 'stream',
        timeout: 300000, // 5分钟超时
        headers: {
          'User-Agent': 'chiwei-bot-cronjob/1.0.0'
        }
      });

      const writer = fs.createWriteStream(zipFilePath);
      response.data.pipe(writer);

      return new Promise((resolve, reject) => {
        writer.on('finish', () => resolve(zipFilePath));
        writer.on('error', reject);
      });
    } catch (error) {
      throw new Error(`下载 ZIP 文件失败: ${error}`);
    }
  }

  /**
   * 解压 ZIP 文件 - 使用流式解压避免内存溢出
   */
  private async extractZipFile(zipFilePath: string, tempDir: string): Promise<string> {
    const extractDir = path.join(tempDir, 'extracted');
    
    try {
      // 确保解压目录存在
      if (!fs.existsSync(extractDir)) {
        fs.mkdirSync(extractDir, { recursive: true });
      }

      console.log('开始流式解压ZIP文件...');
      
      // 使用Promise包装流式解压过程
      await new Promise<void>((resolve, reject) => {
        const stream = fs.createReadStream(zipFilePath)
          .pipe(unzipper.Extract({ path: extractDir }));

        stream.on('close', () => {
          console.log('ZIP文件解压完成');
          resolve();
        });

        stream.on('error', (error: Error) => {
          console.error('解压过程中发生错误:', error);
          reject(error);
        });

        // 添加超时处理（30分钟）
        const timeout = setTimeout(() => {
          stream.destroy();
          reject(new Error('解压超时（30分钟）'));
        }, 30 * 60 * 1000);

        stream.on('close', () => {
          clearTimeout(timeout);
        });

        stream.on('error', () => {
          clearTimeout(timeout);
        });
      });

      return extractDir;
    } catch (error) {
      throw new Error(`解压 ZIP 文件失败: ${error}`);
    }
  }

  /**
   * 处理所有 jsonlines 文件
   */
  private async processJsonlinesFiles(extractedDir: string): Promise<void> {
    await mongoInitPromise; // 确保 MongoDB 已初始化

    const files = fs.readdirSync(extractedDir);
    const jsonlinesFiles = files.filter(file => 
      file.endsWith('.jsonlines') && !file.endsWith('.json') // 排除 .json 文件
    );

    console.log(`找到 ${jsonlinesFiles.length} 个 jsonlines 文件: ${jsonlinesFiles.join(', ')}`);

    for (const fileName of jsonlinesFiles) {
      const filePath = path.join(extractedDir, fileName);
      await this.processJsonlinesFile(filePath, fileName);
    }
  }

  /**
   * 处理单个 jsonlines 文件
   */
  private async processJsonlinesFile(filePath: string, fileName: string): Promise<void> {
    const collection = this.getCollection(fileName);
    if (!collection) {
      console.warn(`未找到文件 ${fileName} 对应的集合，跳过处理`);
      return;
    }

    console.log(`开始处理文件 ${fileName} -> 集合 ${collection.collectionName}`);

    try {
      // 获取文件大小以估算处理时间
      const stats = fs.statSync(filePath);
      const fileSizeMB = (stats.size / (1024 * 1024)).toFixed(2);
      console.log(`文件大小: ${fileSizeMB} MB`);

      // 创建读取流
      const fileStream = createReadStream(filePath, { encoding: 'utf8' });
      const rl = createInterface({
        input: fileStream,
        crlfDelay: Infinity
      });

      const batchSize = 500; // 批量 upsert 大小
      let batch: any[] = [];
      let totalProcessed = 0;

      for await (const line of rl) {
        if (line.trim()) {
          try {
            const document = JSON.parse(line);
            batch.push(document);

            if (batch.length >= batchSize) {
              const uniqueFields = this.getUniqueFields(fileName);
              const bulkOps = this.createBulkOperations(batch, uniqueFields);
              const result = await collection.bulkWrite(bulkOps, { ordered: false });
              totalProcessed += batch.length;
              console.log(`${collection.collectionName}: 已处理 ${totalProcessed} 条记录 ` +
                `(新增: ${result.upsertedCount}, 更新: ${result.modifiedCount}, 匹配: ${result.matchedCount})`);
              batch = [];
            }
          } catch (parseError) {
            console.warn(`解析行数据失败 (行 ${totalProcessed + batch.length + 1}):`, parseError);
          }
        }
      }

      // 处理剩余的批次
      if (batch.length > 0) {
        const uniqueFields = this.getUniqueFields(fileName);
        const bulkOps = this.createBulkOperations(batch, uniqueFields);
        const result = await collection.bulkWrite(bulkOps, { ordered: false });
        totalProcessed += batch.length;
        console.log(`${collection.collectionName}: 最终批次处理 ${batch.length} 条记录 ` +
          `(新增: ${result.upsertedCount}, 更新: ${result.modifiedCount}, 匹配: ${result.matchedCount})`);
      }

      console.log(`${collection.collectionName}: 总共处理了 ${totalProcessed} 条记录`);

    } catch (error) {
      console.error(`处理文件 ${fileName} 时发生错误:`, error);
      throw error;
    }
  }

  /**
   * 根据文件名获取对应的集合实例
   */
  private getCollection(fileName: string): Collection | null {
    const collectionMap: { [key: string]: Collection } = {
      'subject.jsonlines': BangumiSubjectCollection,
      'character.jsonlines': BangumiCharacterCollection,
      'person.jsonlines': BangumiPersonCollection,
      'episode.jsonlines': BangumiEpisodeCollection,
      'subject-characters.jsonlines': BangumiSubjectCharacterCollection,
      'subject-persons.jsonlines': BangumiSubjectPersonCollection,
      'person-characters.jsonlines': BangumiPersonCharacterCollection,
      'subject-relations.jsonlines': BangumiSubjectRelationCollection
    };
    
    return collectionMap[fileName];
  }

  /**
   * 根据文件名获取唯一标识符字段
   */
  private getUniqueFields(fileName: string): string[] {
    const uniqueFieldsMap: { [key: string]: string[] } = {
      'subject.jsonlines': ['id'],
      'character.jsonlines': ['id'],
      'person.jsonlines': ['id'],
      'episode.jsonlines': ['id'],
      'subject-characters.jsonlines': ['subject_id', 'character_id'],
      'subject-persons.jsonlines': ['subject_id', 'person_id'],
      'person-characters.jsonlines': ['person_id', 'character_id'],
      'subject-relations.jsonlines': ['subject_id', 'relation_subject_id']
    };
    
    return uniqueFieldsMap[fileName] || ['id'];
  }

  /**
   * 创建 bulkWrite 操作数组
   */
  private createBulkOperations(batch: any[], uniqueFields: string[]): any[] {
    return batch.map(document => {
      const filter: any = {};
      uniqueFields.forEach(field => {
        filter[field] = document[field];
      });
      
      return {
        updateOne: {
          filter,
          update: { $set: document },
          upsert: true
        }
      };
    });
  }



  /**
   * 清理临时目录
   */
  private async cleanupTempDirectory(tempDir: string): Promise<void> {
    try {
      if (fs.existsSync(tempDir)) {
        fs.rmSync(tempDir, { recursive: true, force: true });
      }
    } catch (error) {
      console.warn('清理临时目录时发生错误:', error);
    }
  }
}

/**
 * 导出主要的同步函数，供定时任务使用
 */
export async function syncBangumiArchive(): Promise<void> {
  const service = new BangumiArchiveService();
  await service.syncBangumiArchiveData();
}
