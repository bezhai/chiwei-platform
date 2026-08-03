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

            // 关闭 Redis 连接：必须 await，resetRedisClient 要 quit 命令连接和订阅
            // 连接两条。不等的话这行日志会先于连接断开打印，而 SIGTERM handler 紧接着
            // 就 process.exit，订阅连接来不及收尾。
            const { resetRedisClient } = await import('@inner/shared/cache');
            await resetRedisClient();
            console.info('Redis connections closed');
        } catch (error) {
            console.warn('Error while closing database connections:', error);
        }
    }
}
