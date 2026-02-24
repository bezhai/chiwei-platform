import { MongoClient, Db, Collection, Document } from 'mongodb';
import { MongoConfig, createDefaultMongoConfig, buildMongoUrl } from './types';
import { MongoCollection } from './collection';

/**
 * MongoDB 客户端封装类
 * 提供连接管理和集合访问功能
 */
export class MongoService {
    private client: MongoClient | null = null;
    private db: Db | null = null;
    private config: MongoConfig;
    private collections: Map<string, MongoCollection<any>> = new Map();
    private initialized: boolean = false;

    constructor(config?: MongoConfig) {
        this.config = config || createDefaultMongoConfig();
    }

    /**
     * 初始化 MongoDB 连接
     */
    async initialize(): Promise<void> {
        if (this.initialized) {
            return;
        }

        try {
            const url = buildMongoUrl(this.config);
            this.client = new MongoClient(url);
            await this.client.connect();
            this.db = this.client.db(this.config.database);
            this.initialized = true;
            console.info(`MongoDB connected to ${this.config.host}/${this.config.database}`);
        } catch (err) {
            console.error('MongoDB initialization failed:', err);
            throw err;
        }
    }

    /**
     * 检查是否已初始化
     */
    isInitialized(): boolean {
        return this.initialized;
    }

    /**
     * 获取数据库实例
     */
    getDb(): Db {
        if (!this.db) {
            throw new Error('MongoDB not initialized. Call initialize() first.');
        }
        return this.db;
    }

    /**
     * 获取原生 Collection
     */
    getNativeCollection<T extends Document = Document>(name: string): Collection<T> {
        if (!this.db) {
            throw new Error('MongoDB not initialized. Call initialize() first.');
        }
        return this.db.collection<T>(name);
    }

    /**
     * 获取封装的 MongoCollection
     */
    getCollection<T extends Document>(name: string): MongoCollection<T> {
        if (!this.db) {
            throw new Error('MongoDB not initialized. Call initialize() first.');
        }

        if (!this.collections.has(name)) {
            const collection = new MongoCollection<T>(this.db.collection<T>(name));
            this.collections.set(name, collection);
        }

        return this.collections.get(name) as MongoCollection<T>;
    }

    /**
     * 关闭连接
     */
    async close(): Promise<void> {
        if (this.client) {
            await this.client.close();
            this.client = null;
            this.db = null;
            this.collections.clear();
            this.initialized = false;
            console.info('MongoDB connection closed');
        }
    }

    /**
     * 健康检查
     */
    async ping(): Promise<boolean> {
        if (!this.db) {
            return false;
        }
        try {
            await this.db.command({ ping: 1 });
            return true;
        } catch {
            return false;
        }
    }
}

// 默认单例实例
let defaultInstance: MongoService | null = null;

/**
 * 获取默认 MongoDB 服务实例
 */
export function getMongoService(config?: MongoConfig): MongoService {
    if (!defaultInstance) {
        defaultInstance = new MongoService(config);
    }
    return defaultInstance;
}

/**
 * 重置默认 MongoDB 服务实例
 */
export async function resetMongoService(): Promise<void> {
    if (defaultInstance) {
        await defaultInstance.close();
        defaultInstance = null;
    }
}

/**
 * 创建并初始化 MongoDB 服务
 */
export async function createMongoService(config?: MongoConfig): Promise<MongoService> {
    const service = new MongoService(config);
    await service.initialize();
    return service;
}
