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

/**
 * 飞书 webhook 路由的挂载方。**必填**：这个进程存在的理由就是接飞书消息，
 * 让它变成可选就等于允许"服务起来了、健康检查绿的、但一条路由都没有"。
 */
export interface WebhookMount {
    registerWebhooks(app: Hono): void;
}

/**
 * 长连入口的真实就绪情况。**必填**，而且必须是一个函数：连接状态一直在变（断线、
 * 重连），装配那一刻的快照没有意义。
 */
export interface IngressStatus {
    expected: number;
    connected: number;
    bots: Array<{ botName: string; state: string }>;
}

export interface LarkServiceAppInput {
    bots: BotRoster;
    inbound: WebhookMount;
    ingress: () => IngressStatus;
}

export function createLarkServiceApp(input: LarkServiceAppInput): Hono {
    const app = new Hono();

    app.onError(errorHandler);
    app.use('*', metricsMiddleware); // 最外层：失败的请求也要被计数
    app.use('*', traceMiddleware);

    app.route('', metricsRoutes);

    // 飞书 webhook 挂在中间件之后：每个 bot 的入站路由自动继承 trace、错误处理和
    // 指标采集，不必各自再接一遍。
    input.inbound.registerWebhooks(app);

    // 存活。**一直 200** —— 长连抖一下不该让 Pod 被重启掉。能不能接飞书事件是另一个
    // 问题，问 /api/ready。
    //
    // bot 清单是排查"这个 Pod 到底在替哪个飞书应用干活"的第一现场：泳道部署最常见的
    // 问题就是绑错 bot 或者 common_user_id 没分配。
    app.get('/api/health', (c) =>
        c.json({
            status: 'ok',
            timestamp: new Date().toISOString(),
            service: 'lark-service',
            version: process.env.VERSION || process.env.GIT_SHA || 'unknown',
            websockets: input.ingress(),
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

    // 就绪 = **真的在接飞书事件**，不是"进程起来了"。
    //
    // 这条判据是切流的地基：切流时先看新服务 ready 再停旧入口。飞书 SDK 的长连
    // start() 只是异步发起重连、不等连上，所以"启动函数返回了"是个假绿灯 —— 照着它
    // 切流会在新服务没连上的情况下停掉旧入口，那段时间消息无人接收且无告警。
    //
    // 不持有长连的部署（webhook-only、泳道部署）expected 是 0，直接就绪 —— 那是合法
    // 姿态，不是"没连上"。
    app.get('/api/ready', (c) => {
        const websockets = input.ingress();
        const ready = websockets.connected === websockets.expected;
        return c.json(
            {
                status: ready ? 'ready' : 'not-ready',
                service: 'lark-service',
                websockets,
            },
            ready ? 200 : 503,
        );
    });

    return app;
}
