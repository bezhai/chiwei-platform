import { DatabaseManager } from './database';
import { HttpServerManager, ServerConfig } from './server';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import { botInitialization } from './initializers/bot';
import { initializeLarkClients } from '@integrations/lark-client';
import { initializeCrontabs } from '@crontab/index';
import { rabbitmqClient, getLane } from '@integrations/rabbitmq';
import { startInboundLaneConsumer } from '@integrations/inbound-lane-consumer';
import { larkEventHandlers } from '@lark/events/handlers';
import { LarkEventIngress } from '@plugins/lark/webhook/ingress';
import { isDirectIngressEnabled } from '@plugins/lark/webhook/ingress-gate';
import { larkIngressBots } from './lark-ingress-bots';

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
    private larkIngress?: LarkEventIngress;

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
            await startInboundLaneConsumer(lane, (params) =>
                larkEventHandlers.handleMessageReceive(params as never),
            );
            console.info(`[inbound-lane] consumer started for lane=${lane}`);
        }

        // 5.6 飞书直连 ws 入口（websocket bot）。HTTP webhook 入口已由本服务承接；
        // WS 长连仍显式用部署开关控制，避免未准备好的 bot 被当前进程主动接管。
        if (isDirectIngressEnabled()) {
            this.larkIngress = new LarkEventIngress();
            const wsBots = larkIngressBots(multiBotManager.getBotsByInitType('websocket'));
            await this.larkIngress.startWebSocketBots(wsBots);
            console.info(`[ingress] direct lark ws ON: started ${wsBots.length} ws bot(s)`);
        }

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
        // 启动 HTTP 服务（包含 Lark webhook 入口）
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
            // 关闭飞书 ws 长连（如启用了直连入口）
            this.larkIngress?.closeWebSocketClients();
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
                `  - ${bot.bot_name} [${bot.channel}] (${appId}) [${bot.init_type}]`,
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
