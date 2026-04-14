import { Hono } from 'hono';
import { cors } from 'hono/cors';
import { bodyLimit } from 'hono/body-limit';
import { errorHandler } from '@middleware/error-handler';
import { traceMiddleware } from '@middleware/trace';
import { botContextMiddleware } from '@middleware/bot-context';
import { createContextPropagationMiddleware } from '@inner/shared/middleware';
import { metricsMiddleware, metricsApp } from '@middleware/metrics';
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
    private app: Hono;
    private config: ServerConfig;

    constructor(
        config: ServerConfig = {
            port: 3000,
        },
    ) {
        this.config = config;
        this.app = new Hono();
        this.app.onError(errorHandler); // 统一错误处理（Hono 原生 onError）
        this.setupMiddleware();
    }

    /**
     * 设置中间件
     */
    private setupMiddleware(): void {
        this.app.use('*', metricsMiddleware); // Prometheus metrics（最外层）
        this.app.use('*', cors());
        this.app.use('*', traceMiddleware); // 先注入 traceId（为后续日志与错误处理提供上下文）
        this.app.use('*', createContextPropagationMiddleware()); // x-ctx-* header 透传
        this.app.use('*', botContextMiddleware); // 注入 botName
        this.app.use('*', bodyLimit({
            maxSize: 50 * 1024 * 1024, // 50mb
        }));
    }

    /**
     * 注册健康检查端点
     */
    private registerHealthCheck(): void {
        this.app.get('/api/health', (c) => {
            try {
                const allBots = multiBotManager.getAllBotConfigs();
                return c.json({
                    status: 'ok',
                    timestamp: new Date().toISOString(),
                    service: 'lark-server',
                    version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
                    bots: allBots.map((bot) => ({
                        name: bot.bot_name,
                        app_id: bot.app_id,
                        init_type: bot.init_type,
                        is_active: bot.is_active,
                    })),
                }, 200);
            } catch (error) {
                return c.json({
                    status: 'error',
                    message: error instanceof Error ? error.message : 'Unknown error',
                }, 500);
            }
        });
    }

    /**
     * 启动 HTTP 服务器
     */
    async start(): Promise<void> {
        // 注册 /metrics 和健康检查路由
        this.app.route('', metricsApp);
        this.registerHealthCheck();
        this.app.route('', internalLarkRoutes);

        // 启动服务器
        Bun.serve({ port: this.config.port, fetch: this.app.fetch });
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
    }

    /**
     * 获取 Hono 应用实例（用于测试）
     */
    getApp(): Hono {
        return this.app;
    }
}
