import { describe, it, expect, beforeEach, afterEach, mock } from 'bun:test';
import * as Lark from '@larksuiteoapi/node-sdk';
import { BotManager } from '../src/bot-manager';
import { EventForwarder } from '../src/forwarder';

describe('BotManager', () => {
    let originalNodeEnv: string | undefined;

    beforeEach(() => {
        originalNodeEnv = process.env.NODE_ENV;
    });

    afterEach(() => {
        if (originalNodeEnv === undefined) {
            delete process.env.NODE_ENV;
        } else {
            process.env.NODE_ENV = originalNodeEnv;
        }
    });

    it('registers HTTP bots and starts WebSocket bots', async () => {
        process.env.NODE_ENV = 'production';

        const pool = {
            query: mock(() =>
                Promise.resolve({
                    rows: [
                        botConfig({ bot_name: 'http-bot', init_type: 'http' }),
                        botConfig({ bot_name: 'ws-bot', init_type: 'websocket' }),
                    ],
                }),
            ),
        };
        const forwarder = {
            createHandler: mock((botName: string) => () => ({ botName })),
            createCardHandler: mock((botName: string) => () => ({ botName })),
        };
        const app = {
            post: mock(() => {}),
        };
        const wsClient = {
            start: mock(() => Promise.resolve()),
            close: mock(() => {}),
        };
        const createWebSocketClient = mock(() => wsClient);

        const manager = new BotManager(
            pool as any,
            forwarder as unknown as EventForwarder,
            createWebSocketClient,
        );

        await manager.registerBots(app as any);

        expect(app.post).toHaveBeenCalledTimes(2);
        expect(app.post).toHaveBeenCalledWith('/webhook/http-bot/event', expect.any(Function));
        expect(app.post).toHaveBeenCalledWith('/webhook/http-bot/card', expect.any(Function));
        expect(createWebSocketClient).toHaveBeenCalledTimes(1);
        expect(createWebSocketClient).toHaveBeenCalledWith(
            expect.objectContaining({ bot_name: 'ws-bot' }),
        );
        expect(wsClient.start).toHaveBeenCalledWith({
            eventDispatcher: expect.any(Lark.EventDispatcher),
        });
    });

    it('reads credentials from the credentials JSONB column, not dropped bare columns', async () => {
        // bot_config 多 channel 化后旧裸列 (app_id/app_secret/encrypt_key/
        // verification_token) 已删，再 SELECT 它们运行期会 "column does not
        // exist"。加载必须改成查 channel + credentials JSONB。
        const querySpy = mock(() =>
            Promise.resolve({
                rows: [
                    {
                        bot_name: 'lark-bot',
                        channel: 'lark',
                        credentials: {
                            app_id: 'cred-app-id',
                            app_secret: 'cred-app-secret',
                            encrypt_key: 'cred-encrypt-key',
                            verification_token: 'cred-verification-token',
                            robot_union_id: 'cred-union',
                        },
                        init_type: 'websocket',
                        is_active: true,
                        is_dev: false,
                    },
                ],
            }),
        );
        const pool = { query: querySpy };
        const forwarder = {
            createHandler: mock(() => () => ({})),
            createCardHandler: mock(() => () => ({})),
        };
        const wsClient = {
            start: mock(() => Promise.resolve()),
            close: mock(() => {}),
        };
        const createWebSocketClient = mock(() => wsClient);
        const manager = new BotManager(
            pool as any,
            forwarder as unknown as EventForwarder,
            createWebSocketClient,
        );

        await manager.registerBots({ post: mock(() => {}) } as any);

        const sql = String(querySpy.mock.calls[0][0]);
        // 不允许再 SELECT 已删的裸凭据列
        expect(sql).not.toMatch(/\bapp_secret\b/);
        expect(sql).not.toMatch(/\bencrypt_key\b/);
        expect(sql).not.toMatch(/\bverification_token\b/);
        // 必须查 credentials JSONB（裸列已不存在）
        expect(sql).toMatch(/\bcredentials\b/);
        // 凭据从 credentials JSONB 解释出来，行为与旧裸列等价
        expect(createWebSocketClient).toHaveBeenCalledWith(
            expect.objectContaining({ app_id: 'cred-app-id' }),
        );
    });

    it('skips non-lark bots in lark-proxy (lark webhook ingress only)', async () => {
        const pool = {
            query: mock(() =>
                Promise.resolve({
                    rows: [
                        {
                            bot_name: 'qq-bot',
                            channel: 'qq',
                            credentials: { app_id: 'qq-app', app_secret: 'qq-sec' },
                            init_type: 'http',
                            is_active: true,
                            is_dev: false,
                        },
                    ],
                }),
            ),
        };
        const forwarder = {
            createHandler: mock(() => () => ({})),
            createCardHandler: mock(() => () => ({})),
        };
        const app = { post: mock(() => {}) };
        const manager = new BotManager(
            pool as any,
            forwarder as unknown as EventForwarder,
            mock(() => ({ start: mock(() => Promise.resolve()), close: mock(() => {}) })),
        );

        await manager.registerBots(app as any);

        // qq bot 不是飞书 webhook，lark-proxy 不该为它注册任何路由
        expect(app.post).not.toHaveBeenCalled();
    });

    it('closes started WebSocket clients', async () => {
        const pool = {
            query: mock(() =>
                Promise.resolve({
                    rows: [botConfig({ bot_name: 'ws-bot', init_type: 'websocket' })],
                }),
            ),
        };
        const forwarder = {
            createHandler: mock(() => () => ({})),
            createCardHandler: mock(() => () => ({})),
        };
        const wsClient = {
            start: mock(() => Promise.resolve()),
            close: mock(() => {}),
        };
        const manager = new BotManager(
            pool as any,
            forwarder as unknown as EventForwarder,
            mock(() => wsClient),
        );

        await manager.registerBots({ post: mock(() => {}) } as any);
        manager.closeWebSocketClients();

        expect(wsClient.close).toHaveBeenCalledWith({ force: true });
    });
});

function botConfig(overrides: Partial<Record<string, unknown>>) {
    return {
        bot_name: 'bot',
        channel: 'lark',
        credentials: {
            app_id: 'app-id',
            app_secret: 'app-secret',
            encrypt_key: 'encrypt-key',
            verification_token: 'verification-token',
            robot_union_id: 'union-id',
        },
        init_type: 'http',
        is_active: true,
        is_dev: false,
        ...overrides,
    };
}
