import Koa from 'koa';
import Router from '@koa/router';
import koaBody from 'koa-body';
import cors from '@koa/cors';
import { errorHandler } from '@middleware/error-handler';
import { traceMiddleware } from '@middleware/trace';
import { botContextMiddleware } from '@middleware/bot-context';
import imageProcessRoutes from '@api/routes/image.route';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';
import internalLarkRoutes from '@api/routes/internal-lark.route';

/**
 * 服务器配置
 */
export interface ServerConfig {
    port: number;
}

/**
 * HTTP 服务器管理器
 */
export class HttpServerManager {
    private app: Koa;
    private router: Router;
    private config: ServerConfig;

    constructor(
        config: ServerConfig = {
            port: 3000,
        },
    ) {
        this.config = config;
        this.app = new Koa();
        this.router = new Router();
        this.setupMiddleware();
    }

    /**
     * 设置中间件
     */
    private setupMiddleware(): void {
        this.app.use(cors());
        this.app.use(traceMiddleware); // 先注入 traceId（为后续日志与错误处理提供上下文）
        this.app.use(errorHandler); // 统一错误处理（依赖 traceId 贯穿）
        this.app.use(botContextMiddleware); // 注入 botName
        this.app.use(koaBody({
            formLimit: '50mb',
            jsonLimit: '50mb',
            textLimit: '50mb',
            multipart: true,
        }));
    }

    /**
     * 注册健康检查端点
     */
    private registerHealthCheck(): void {
        this.router.get('/api/health', (ctx) => {
            try {
                const allBots = multiBotManager.getAllBotConfigs();
                ctx.body = {
                    status: 'ok',
                    timestamp: new Date().toISOString(),
                    service: 'lark-server',
                    version: process.env.GIT_SHA || 'unknown',
                    bots: allBots.map((bot) => ({
                        name: bot.bot_name,
                        app_id: bot.app_id,
                        init_type: bot.init_type,
                        is_active: bot.is_active,
                    })),
                };
                ctx.status = 200;
            } catch (error) {
                ctx.body = {
                    status: 'error',
                    message: error instanceof Error ? error.message : 'Unknown error',
                };
                ctx.status = 500;
            }
        });
    }

    /**
     * 启动 HTTP 服务器
     */
    async start(): Promise<void> {
        // 注册健康检查和其他路由
        this.registerHealthCheck();
        this.app.use(this.router.routes());
        this.app.use(internalLarkRoutes.routes());
        this.app.use(imageProcessRoutes.routes());

        // 启动服务器
        this.app.listen(this.config.port);
        console.info(`HTTP server started on port ${this.config.port}`);
        this.logAvailableRoutes();
    }

    /**
     * 记录可用路由
     */
    private logAvailableRoutes(): void {
        console.info('Available routes:');
        console.info('  - /api/health (health check)');
        console.info('  - /api/internal/lark-event (lane-proxy forwarded events)');
        console.info('  - /api/image/process (image processing)');
        console.info('  - /api/image/upload-base64 (base64 image upload)');
    }

    /**
     * 获取 Koa 应用实例（用于测试）
     */
    getApp(): Koa {
        return this.app;
    }
}
