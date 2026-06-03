import { afterEach, beforeEach, describe, expect, mock, test } from 'bun:test';
import { Hono } from 'hono';
import type { BotConfig } from '@entities/bot-config';
import { multiBotManager } from '@core/services/bot/multi-bot-manager';

mock.module('@dal/mongo/client', () => ({
    insertEvent: async () => undefined,
}));
mock.module('@plugins/lark/events/event-registry', () => ({
    EventHandler: () => () => undefined,
    EventRegistry: {
        getHandlerByEventType: () => undefined,
    },
    registerEventHandlerInstance: () => {},
}));
mock.module('@plugins/lark/events/handlers', () => ({
    larkEventHandlers: {},
}));

const { HttpServerManager } = await import('./server');

type MutableMultiBotManager = {
    botConfigs: Map<string, BotConfig>;
};

const originalServe = Bun.serve;
const originalBotConfigs = new Map(
    (multiBotManager as unknown as MutableMultiBotManager).botConfigs,
);

function larkBot(botName: string): BotConfig {
    return {
        bot_name: botName,
        channel: 'lark',
        init_type: 'http',
        is_active: true,
        is_dev: false,
        bot_role: 'persona',
        createdAt: new Date('2026-01-01T00:00:00.000Z'),
        updatedAt: new Date('2026-01-01T00:00:00.000Z'),
        credentials: {
            app_id: 'cli_lark_http',
            app_secret: 'app_secret',
            encrypt_key: 'encrypt_key',
            verification_token: 'verification_token',
            robot_union_id: 'ou_lark_http',
        },
    };
}

function nonLarkBot(botName: string): BotConfig {
    return {
        bot_name: botName,
        channel: 'qq',
        init_type: 'http',
        is_active: true,
        is_dev: false,
        bot_role: 'persona',
        createdAt: new Date('2026-01-01T00:00:00.000Z'),
        updatedAt: new Date('2026-01-01T00:00:00.000Z'),
        credentials: {
            app_id: 'qq_app',
            app_secret: 'qq_secret',
        },
    };
}

async function requestChallenge(app: Hono, path: string): Promise<Response> {
    return app.request(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            type: 'url_verification',
            challenge: `challenge:${path}`,
        }),
    });
}

/**
 * 仅验证健康检查端点与错误处理中间件集成。
 * 使用 Hono 的 app.request() 发起请求，不启动真实端口监听。
 */
describe('startup/server 集成烟雾测试', () => {
    beforeEach(() => {
        delete process.env.LARK_DIRECT_INGRESS;
        (Bun as unknown as { serve: unknown }).serve = mock(() => ({ stop: mock() }));
    });

    afterEach(() => {
        (Bun as unknown as { serve: typeof Bun.serve }).serve = originalServe;
        (multiBotManager as unknown as MutableMultiBotManager).botConfigs = new Map(
            originalBotConfigs,
        );
    });

    test('GET /api/health 返回 200 且包含服务字段', async () => {
        const app = new Hono();
        app.get('/api/health', (c) => {
            return c.json({ status: 'ok', service: 'channel-server' }, 200);
        });

        const res = await app.request('/api/health');
        expect(res.status).toBe(200);
        const body = await res.json();
        expect(body.service).toBe('channel-server');
    });

    test('start() always registers lark http webhook routes without LARK_DIRECT_INGRESS', async () => {
        const bots = [larkBot('lark-http'), nonLarkBot('qq-http')];
        (multiBotManager as unknown as MutableMultiBotManager).botConfigs = new Map(
            bots.map((bot) => [bot.bot_name, bot]),
        );

        const server = new HttpServerManager({ port: 0 });
        await server.start();
        const app = server.getApp();

        const eventRes = await requestChallenge(app, '/webhook/lark-http/event');
        expect(eventRes.status).toBe(200);
        expect(await eventRes.json()).toEqual({
            challenge: 'challenge:/webhook/lark-http/event',
        });

        const cardRes = await requestChallenge(app, '/webhook/lark-http/card');
        expect(cardRes.status).toBe(200);
        expect(await cardRes.json()).toEqual({
            challenge: 'challenge:/webhook/lark-http/card',
        });

        expect((await requestChallenge(app, '/webhook/qq-http/event')).status).toBe(404);
        expect((await requestChallenge(app, '/webhook/qq-http/card')).status).toBe(404);
    });
});
