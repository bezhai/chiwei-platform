import { DatabaseManager } from './database';
import { HttpServerManager, ServerConfig } from './server';
import { botDirectory } from '@inner/shared/bot';
import { rabbitmqClient } from '@inner/shared/mq';
import '@plugins/index';
import {
    initializeChannelRuntimes,
    runChannelInitializers,
    shutdownChannelRuntimes,
    startChannelDirectIngresses,
} from '@plugins/runtime';

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

        // 2. 加载本服务负责的 bot 身份
        await botDirectory.load();
        console.info('Bot directory loaded!');

        // 3. 初始化各 channel runtime（平台 SDK client 等）
        await initializeChannelRuntimes();
        console.info('Channel runtimes initialized!');

        // 4. 运行各 channel runtime 的可选初始化任务（如 NEED_INIT=true 的群信息同步）
        await runChannelInitializers();
        console.info('Channel runtime initializers completed!');

        // 5. 连接 RabbitMQ（入站消息写入后的 ChatTrigger 发布需要）
        await rabbitmqClient.connect();
        await rabbitmqClient.declareTopology();
        console.info('RabbitMQ connected!');

        // 6. 各 channel runtime 自己决定是否启动主动入口（如平台 WS）。
        //
        // 泳道交接的接收端不在这里起：它是一条 HTTP 路由，由各 runtime 的
        // registerHttpIngress 跟自己的原始入站端点一起挂上，**每个部署都挂**（prod 也
        // 挂）—— 泳道的 Service 不存在时 sidecar 会把交接打回 prod 自己。
        await startChannelDirectIngresses(botDirectory.getBotsByInitType('websocket'));

        // 7. 显示当前加载的机器人配置
        this.logBotConfigurations();

        console.info('Application initialization completed!');
    }

    /**
     * 启动服务
     */
    async start(): Promise<void> {
        // 启动 HTTP 服务（包含各 channel runtime 注册的 webhook/ingress 入口）
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
            // 关闭各 channel runtime 主动入口（如 WS 长连）
            await shutdownChannelRuntimes();
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
        const allBots = botDirectory.getAllBotConfigs();
        console.info(`Loaded ${allBots.length} bot configurations:`);
        allBots.forEach((bot) => {
            const appId = (bot.credentials?.app_id as string | undefined) ?? '-';
            console.info(
                `  - ${bot.bot_name} [${bot.channel}] (${appId}) ` +
                    `[${bot.init_type}] common_user_id=${bot.common_user_id ?? '-'}`,
            );
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
