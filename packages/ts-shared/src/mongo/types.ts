import { Document } from 'mongodb';

/**
 * MongoDB 配置选项
 */
export interface MongoConfig {
    /** MongoDB 主机地址 */
    host: string;
    /** MongoDB 端口 */
    port?: number;
    /** 用户名 */
    username?: string;
    /** 密码 */
    password?: string;
    /** 数据库名称 */
    database: string;
    /** 认证数据库 */
    authSource?: string;
    /** 连接超时时间（毫秒） */
    connectTimeoutMS?: number;
    /** 额外的连接选项 */
    options?: Record<string, string | number | boolean>;
}

/**
 * 从环境变量创建默认 MongoDB 配置
 */
export function createDefaultMongoConfig(database: string = 'chiwei'): MongoConfig {
    return {
        host: process.env.MONGO_HOST || 'localhost',
        port: parseInt(process.env.MONGO_PORT || '27017', 10),
        username: process.env.MONGO_INITDB_ROOT_USERNAME,
        password: process.env.MONGO_INITDB_ROOT_PASSWORD,
        database,
        authSource: 'admin',
        connectTimeoutMS: 2000,
    };
}

/**
 * 根据配置生成 MongoDB 连接 URL
 */
export function buildMongoUrl(config: MongoConfig): string {
    const { host, port, username, password, database, authSource, connectTimeoutMS, options } = config;

    let url = 'mongodb://';

    // 添加认证信息
    if (username && password) {
        url += `${encodeURIComponent(username)}:${encodeURIComponent(password)}@`;
    }

    // 添加主机和端口
    url += host;
    if (port) {
        url += `:${port}`;
    }

    // 添加数据库
    url += `/${database}`;

    // 构建查询参数
    const params: string[] = [];

    if (connectTimeoutMS) {
        params.push(`connectTimeoutMS=${connectTimeoutMS}`);
    }

    if (authSource) {
        params.push(`authSource=${authSource}`);
    }

    if (options) {
        for (const [key, value] of Object.entries(options)) {
            params.push(`${key}=${value}`);
        }
    }

    if (params.length > 0) {
        url += '?' + params.join('&');
    }

    return url;
}

/**
 * 索引定义
 */
export interface IndexDefinition {
    /** 索引字段 */
    fields: Record<string, 1 | -1>;
    /** 是否唯一索引 */
    unique?: boolean;
    /** 是否后台创建 */
    background?: boolean;
    /** 索引名称 */
    name?: string;
    /** 是否稀疏索引 */
    sparse?: boolean;
    /** 过期时间（秒），用于 TTL 索引 */
    expireAfterSeconds?: number;
}

/**
 * 批量操作结果
 */
export interface BulkWriteResult {
    insertedCount: number;
    matchedCount: number;
    modifiedCount: number;
    deletedCount: number;
    upsertedCount: number;
}
