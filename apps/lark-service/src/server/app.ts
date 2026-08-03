/**
 * 本服务的 HTTP 装配点。骨架阶段只有健康检查和 metrics，飞书的 webhook / 泳道
 * 入口后续挂到同一个 app 上，自动继承这里的 trace、错误处理和指标采集。
 */

import { Hono } from 'hono';
import type { BotConfig } from '@inner/shared/entities';
import { errorHandler, traceMiddleware } from '@inner/shared/middleware';

import { metricsMiddleware, metricsRoutes } from './metrics';

/** 健康检查要报"我在驱动哪些 bot"，只需要读这一件事，不必依赖整个 BotDirectory。 */
export interface BotRoster {
    getAllBotConfigs(): BotConfig[];
}

export interface LarkServiceAppInput {
    bots: BotRoster;
}

export function createLarkServiceApp(input: LarkServiceAppInput): Hono {
    const app = new Hono();

    app.onError(errorHandler);
    app.use('*', metricsMiddleware); // 最外层：失败的请求也要被计数
    app.use('*', traceMiddleware);

    app.route('', metricsRoutes);

    // bot 清单是排查"这个 Pod 到底在替哪个飞书应用干活"的第一现场：泳道部署最常
    // 见的问题就是绑错 bot 或者 common_user_id 没分配。
    app.get('/api/health', (c) =>
        c.json({
            status: 'ok',
            timestamp: new Date().toISOString(),
            service: 'lark-service',
            version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
            bots: input.bots.getAllBotConfigs().map((bot) => ({
                name: bot.bot_name,
                channel: bot.channel,
                app_id: bot.credentials?.app_id as string | undefined,
                common_user_id: bot.common_user_id,
                init_type: bot.init_type,
                is_active: bot.is_active,
            })),
        }),
    );

    return app;
}
