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
        app_id: 'app-id',
        app_secret: 'app-secret',
        encrypt_key: 'encrypt-key',
        verification_token: 'verification-token',
        init_type: 'http',
        is_active: true,
        is_dev: false,
        ...overrides,
    };
}
