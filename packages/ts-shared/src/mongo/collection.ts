import {
    Collection,
    Filter,
    InsertOneOptions,
    UpdateOptions,
    DeleteOptions,
    CountDocumentsOptions,
    FindOptions,
    Document,
    WithId,
    OptionalUnlessRequiredId,
    MatchKeysAndValues,
    UpdateFilter,
    BulkWriteOptions,
    AnyBulkWriteOperation,
    IndexSpecification,
    CreateIndexesOptions,
} from 'mongodb';
import { BulkWriteResult, IndexDefinition } from './types';

/**
 * MongoDB 集合泛型封装类
 * 提供类型安全的数据库操作接口
 */
export class MongoCollection<T extends Document> {
    private collection: Collection<T>;

    constructor(collection: Collection<T>) {
        this.collection = collection;
    }

    /**
     * 获取原生 Collection 对象
     */
    getNativeCollection(): Collection<T> {
        return this.collection;
    }

    /**
     * 查询单个文档
     */
    async findOne(filter: Filter<T>, options?: FindOptions<T>): Promise<WithId<T> | null> {
        return this.collection.findOne(filter, options);
    }

    /**
     * 查询多个文档
     */
    async find(filter: Filter<T>, options?: FindOptions<T>): Promise<WithId<T>[]> {
        const cursor = this.collection.find(filter, options);
        return cursor.toArray();
    }

    /**
     * 插入单个文档
     */
    async insertOne(doc: OptionalUnlessRequiredId<T>, options?: InsertOneOptions): Promise<void> {
        await this.collection.insertOne(doc, options);
    }

    /**
     * 插入多个文档
     */
    async insertMany(docs: OptionalUnlessRequiredId<T>[], options?: InsertOneOptions): Promise<void> {
        await this.collection.insertMany(docs, options);
    }

    /**
     * 更新单个文档（使用 $set）
     */
    async updateOne(filter: Filter<T>, update: Partial<T>, options?: UpdateOptions): Promise<void> {
        await this.collection.updateOne(filter, { $set: update }, options);
    }

    /**
     * 使用原生更新操作符更新单个文档
     */
    async updateOneRaw(
        filter: Filter<T>,
        update: Document[] | UpdateFilter<T>,
        options?: UpdateOptions
    ): Promise<void> {
        await this.collection.updateOne(filter, update, options);
    }

    /**
     * 更新多个文档（使用 $set）
     */
    async updateMany(
        filter: Filter<T>,
        update: MatchKeysAndValues<T>,
        options?: UpdateOptions
    ): Promise<void> {
        await this.collection.updateMany(filter, { $set: update }, options);
    }

    /**
     * 删除单个文档
     */
    async deleteOne(filter: Filter<T>, options?: DeleteOptions): Promise<void> {
        await this.collection.deleteOne(filter, options);
    }

    /**
     * 删除多个文档
     */
    async deleteMany(filter: Filter<T>, options?: DeleteOptions): Promise<void> {
        await this.collection.deleteMany(filter, options);
    }

    /**
     * 统计文档数量
     */
    async countDocuments(filter: Filter<T>, options?: CountDocumentsOptions): Promise<number> {
        return this.collection.countDocuments(filter, options);
    }

    /**
     * 批量写入操作
     */
    async bulkWrite(
        operations: AnyBulkWriteOperation<T>[],
        options?: BulkWriteOptions
    ): Promise<BulkWriteResult> {
        const result = await this.collection.bulkWrite(operations, options);
        return {
            insertedCount: result.insertedCount,
            matchedCount: result.matchedCount,
            modifiedCount: result.modifiedCount,
            deletedCount: result.deletedCount,
            upsertedCount: result.upsertedCount,
        };
    }

    /**
     * 创建索引
     */
    async createIndex(
        indexSpec: IndexSpecification,
        options?: CreateIndexesOptions
    ): Promise<string> {
        return this.collection.createIndex(indexSpec, options);
    }

    /**
     * 批量创建索引
     */
    async createIndexes(indexes: IndexDefinition[]): Promise<string[]> {
        const results: string[] = [];
        for (const index of indexes) {
            try {
                const result = await this.collection.createIndex(index.fields, {
                    unique: index.unique,
                    background: index.background,
                    name: index.name,
                    sparse: index.sparse,
                    expireAfterSeconds: index.expireAfterSeconds,
                });
                results.push(result);
            } catch (error: any) {
                // 索引已存在（错误码 85）时跳过
                if (error.code !== 85) {
                    throw error;
                }
            }
        }
        return results;
    }

    /**
     * 聚合查询
     */
    async aggregate<R extends Document = Document>(pipeline: Document[]): Promise<R[]> {
        const cursor = this.collection.aggregate<R>(pipeline);
        return cursor.toArray();
    }

    /**
     * 查找并更新
     */
    async findOneAndUpdate(
        filter: Filter<T>,
        update: UpdateFilter<T>,
        options?: { upsert?: boolean; returnDocument?: 'before' | 'after' }
    ): Promise<WithId<T> | null> {
        const result = await this.collection.findOneAndUpdate(filter, update, {
            upsert: options?.upsert,
            returnDocument: options?.returnDocument,
        });
        return result;
    }

    /**
     * 查找并删除
     */
    async findOneAndDelete(filter: Filter<T>): Promise<WithId<T> | null> {
        const result = await this.collection.findOneAndDelete(filter);
        return result;
    }

    /**
     * 去重查询
     */
    async distinct<K extends keyof WithId<T>>(
        field: K,
        filter?: Filter<T>
    ): Promise<Array<WithId<T>[K]>> {
        return this.collection.distinct(field as string, filter || {}) as Promise<Array<WithId<T>[K]>>;
    }
}
