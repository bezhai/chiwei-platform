import { DatabaseManager } from './database';
import { HttpServerManager, ServerConfig } from './server';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { botInitialization } from './initializers/bot';
import { initializeLarkClients } from '@integrations/lark-client';
import { initializeCrontabs } from '@crontab/index';
import { rabbitmqClient } from '@integrations/rabbitmq';

/**
 * 应用程序配置
 */
export interface ApplicationConfig {
    server: ServerConfig;
}

/**
 * 应用程序管理器
 * 统一管理整个应用的启动和关闭流程
 */
export class ApplicationManager {
    private httpServer?: HttpServerManager;
    private config: ApplicationConfig;

    constructor(config: ApplicationConfig) {
        this.config = config;
    }

    /**
     * 初始化应用程序
     */
    async initialize(): Promise<void> {
        console.info('Starting application initialization...');

        // 1. 初始化数据库
        await DatabaseManager.initialize();

        // 2. 初始化多机器人管理器
        await multiBotManager.initialize();
        console.info('Multi-bot manager initialized!');

        // 3. 初始化 Lark 客户端池
        await initializeLarkClients();
        console.info('Lark client pool initialized!');

        // 4. 初始化机器人
        await botInitialization();
        console.info('Bot initialized successfully!');

        // 5. 连接 RabbitMQ（storeMessage 需要）
        await rabbitmqClient.connect();
        await rabbitmqClient.declareTopology();
        console.info('RabbitMQ connected!');

        // 6. 启动所有定时任务
        initializeCrontabs();
        console.info('All crontab tasks initialized!');

        // 7. 显示当前加载的机器人配置
        this.logBotConfigurations();

        console.info('Application initialization completed!');
    }

    /**
     * 启动服务
     */
    async start(): Promise<void> {
        // 启动 HTTP 服务（Lark webhook 已由 lane-proxy 处理）
        await this.startHttpServer();
    }

    /**
     * 启动 HTTP 服务器
     */
    private async startHttpServer(): Promise<void> {
        this.httpServer = new HttpServerManager(this.config.server);
        await this.httpServer.start();
    }

    /**
     * 优雅关闭应用程序
     */
    async shutdown(): Promise<void> {
        console.info('Gracefully shutting down...');

        try {
            // 关闭 RabbitMQ 连接
            await rabbitmqClient.close();
            // 关闭数据库连接
            await DatabaseManager.close();
            console.info('Application shutdown completed');
        } catch (error) {
            console.error('Error during shutdown:', error);
        }
    }

    /**
     * 记录机器人配置信息
     */
    private logBotConfigurations(): void {
        const allBots = multiBotManager.getAllBotConfigs();
        console.info(`Loaded ${allBots.length} bot configurations:`);
        allBots.forEach((bot) => {
            console.info(`  - ${bot.bot_name} (${bot.app_id}) [${bot.init_type}]`);
        });
    }

    /**
     * 获取 HTTP 服务器实例（用于测试）
     */
    getHttpServer(): HttpServerManager | undefined {
        return this.httpServer;
    }
}

/**
 * 创建默认应用程序配置
 */
export function createDefaultConfig(): ApplicationConfig {
    return {
        server: {
            port: 3000,
        },
    };
}

/**
 * 设置进程信号处理
 */
export function setupProcessHandlers(app: ApplicationManager): void {
    process.on('SIGINT', async () => {
        await app.shutdown();
        process.exit(0);
    });

    process.on('SIGTERM', async () => {
        await app.shutdown();
        process.exit(0);
    });

    process.on('uncaughtException', (error) => {
        console.error('Uncaught Exception:', error);
        process.exit(1);
    });

    process.on('unhandledRejection', (reason, promise) => {
        console.error('Unhandled Rejection at:', promise, 'reason:', reason);
        process.exit(1);
    });
}
