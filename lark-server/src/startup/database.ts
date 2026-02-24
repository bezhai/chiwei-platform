import { mongoInitPromise } from '@dal/mongo/client';
import AppDataSource from '@ormconfig';

/**
 * 数据库初始化管理器
 */
export class DatabaseManager {
    /**
     * 初始化所有数据库连接
     */
    static async initialize(): Promise<void> {
        await Promise.all([mongoInitPromise(), AppDataSource.initialize()]);
        console.info('Database connections established!');
    }

    /**
     * 关闭所有数据库连接
     */
    static async close(): Promise<void> {
        try {
            // 关闭 PostgreSQL 连接
            if (AppDataSource.isInitialized) {
                await AppDataSource.destroy();
                console.info('PostgreSQL connection closed');
            }

            // 关闭 Redis 连接
            const { close } = await import('@cache/redis-client');
            await close();
            console.info('Redis connections closed');
        } catch (error) {
            console.warn('Error while closing database connections:', error);
        }
    }
}
