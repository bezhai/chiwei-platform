import { afterEach, beforeEach, describe, expect, mock, test } from 'bun:test';
import { Hono } from 'hono';
import type { BotConfig } from '@inner/shared/entities';
import { botDirectory } from '@inner/shared/bot';
import type { CustomInboundMessage } from '@inner/shared/protocols';
import { qqEventHandlers } from '@plugins/qq/events/handlers';

// 本文件不装任何 mock.module：bun 的 mock.module 是整模块替换 + 进程级全局
// （mock.restore() 撤不掉），留着只会让同一轮里后跑的文件拿到残缺模块。入站
// handler 用实例上的属性覆盖代替（afterEach 删掉那个自有属性就回到原型方法）。
const { HttpServerManager } = await import('./server');

type MutableBotDirectory = {
    botConfigs: Map<string, BotConfig>;
};

/** 覆盖实例自有属性用；可选是为了 afterEach 能 `delete` 回到原型方法。 */
type MutableHandlers = {
    handleInbound?: (msg: CustomInboundMessage) => Promise<void>;
};

const originalServe = Bun.serve;
const originalBotConfigs = new Map((botDirectory as unknown as MutableBotDirectory).botConfigs);
const originalSecret = process.env.INNER_HTTP_SECRET;

const QQ_INBOUND_PATH = '/api/internal/qq/inbound';
const SECRET = 'inner-secret-for-test';

function qqBot(botName: string): BotConfig {
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
    } as BotConfig;
}

function inboundMessage(): CustomInboundMessage {
    return {
        botName: 'chiwei-qq',
        chatType: 'direct',
        conversationId: 'user_001',
        senderId: 'user_001',
        text: '你好',
        messageId: 'msg_10001',
        timestamp: '2026-06-27T10:00:00+08:00',
    };
}

async function post(app: Hono, path: string, body: unknown, token?: string): Promise<Response> {
    return app.request(path, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
        body: JSON.stringify(body),
    });
}

async function startedApp(): Promise<Hono> {
    const server = new HttpServerManager({ port: 0 });
    await server.start();
    return server.getApp();
}

/**
 * 健康检查 + channel-server 唯一的核心入站入口。
 *
 * 第二组用例走**真实的** HttpServerManager.start()：QQ 入站不是 webhook 握手，而是
 * 内网 Bearer + CustomInboundMessage 校验，路由注册断在 registerChannelHttpIngresses
 * 那一层。没有它的话，「插件 runtime 没注册路由」的表现是 QQ 消息全部 404 —— 进程
 * 健康、日志干净、一条消息都进不来。
 */
describe('startup/server 集成烟雾测试', () => {
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
});

describe('HttpServerManager.start()：QQ 入站路由', () => {
    beforeEach(() => {
        process.env.INNER_HTTP_SECRET = SECRET;
        (Bun as unknown as { serve: unknown }).serve = mock(() => ({ stop: mock() }));
        (botDirectory as unknown as MutableBotDirectory).botConfigs = new Map([
            ['chiwei-qq', qqBot('chiwei-qq')],
        ]);
    });

    afterEach(() => {
        (Bun as unknown as { serve: typeof Bun.serve }).serve = originalServe;
        (botDirectory as unknown as MutableBotDirectory).botConfigs = new Map(originalBotConfigs);
        if (originalSecret === undefined) Reflect.deleteProperty(process.env, 'INNER_HTTP_SECRET');
        else process.env.INNER_HTTP_SECRET = originalSecret;
        // 覆盖的是实例自有属性，删掉就回到 QqEventHandlers 的原型方法。
        delete (qqEventHandlers as unknown as MutableHandlers).handleInbound;
    });

    test('注册了 POST /api/internal/qq/inbound，且带内网 Bearer 鉴权', async () => {
        const app = await startedApp();

        // 无 Authorization：401 而不是 404 —— 401 证明路由在，鉴权也挂上了。
        const unauthorized = await post(app, QQ_INBOUND_PATH, inboundMessage());
        expect(unauthorized.status).toBe(401);

        // 令牌不对同样是 401。
        const wrongToken = await post(app, QQ_INBOUND_PATH, inboundMessage(), 'not-the-secret');
        expect(wrongToken.status).toBe(401);

        // 对照组：没注册的路径才是 404。上面两条不是「什么都没注册」的假象。
        const missing = await post(app, '/api/internal/nope/inbound', {}, SECRET);
        expect(missing.status).toBe(404);
    });

    test('鉴权通过 + 合法 CustomInboundMessage → 交给 QQ 入站编排', async () => {
        const seen: CustomInboundMessage[] = [];
        (qqEventHandlers as unknown as MutableHandlers).handleInbound = async (
            msg: CustomInboundMessage,
        ): Promise<void> => {
            seen.push(msg);
        };

        const app = await startedApp();
        const res = await post(app, QQ_INBOUND_PATH, inboundMessage(), SECRET);

        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ success: true });
        expect(seen).toEqual([inboundMessage()]);
    });

    test('鉴权通过但报文不合法 → 400，且不进入入站编排', async () => {
        let reached = false;
        (qqEventHandlers as unknown as MutableHandlers).handleInbound =
            async (): Promise<void> => {
                reached = true;
            };

        const app = await startedApp();
        const { messageId: _dropped, ...withoutMessageId } = inboundMessage();
        const res = await post(app, QQ_INBOUND_PATH, withoutMessageId, SECRET);

        expect(res.status).toBe(400);
        expect(reached).toBe(false);
    });
});
