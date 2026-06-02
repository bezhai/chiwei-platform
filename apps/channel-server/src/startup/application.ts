import { DatabaseManager } from './database';
import { HttpServerManager, ServerConfig } from './server';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { initializeCrontabs } from '@crontab/index';
import { rabbitmqClient, getLane } from '@integrations/rabbitmq';
import { startInboundLaneConsumer } from '@integrations/inbound-lane-consumer';
import '@plugins/index';
import {
    handleInboundLaneEnvelope,
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

        // 2. 初始化多机器人管理器
        await multiBotManager.initialize();
        console.info('Multi-bot manager initialized!');

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

        // 5.5 lane channel-server 起 inbound_lane.{lane} 消费者（处理层分流接收端）。
        // 仅 lane 部署（getLane() 非空）才起：消费 prod channel-server 投来的本 lane
        // 消息，走与现状一致的入站后半段。prod 部署不起（prod 不消费 inbound_lane.*，
        // §4.2）。与 flag 无关——flag 控制 prod 是否分流，消费端只要是 lane 部署就该
        // 待命（flag off 时队列为空，消费者空转无害）。
        const lane = getLane();
        if (lane) {
            await startInboundLaneConsumer(lane, handleInboundLaneEnvelope);
            console.info(`[inbound-lane] consumer started for lane=${lane}`);
        }

        // 5.6 各 channel runtime 自己决定是否启动主动入口（如平台 WS）。
        await startChannelDirectIngresses(multiBotManager.getBotsByInitType('websocket'));

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
        const allBots = multiBotManager.getAllBotConfigs();
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
