import { describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';

import { createLarkServiceApp, type BotRoster, type IngressStatus, type WebhookMount } from './app';

// webhook 路由由飞书入站装配根挂上来，HTTP 层只提供挂载点。
function inboundThatMounts(paths: string[]): WebhookMount {
    return {
        registerWebhooks(app) {
            for (const path of paths) app.post(path, (c) => c.json({ mounted: path }));
        },
    };
}

function bot(overrides: Partial<BotConfig> = {}): BotConfig {
    return {
        bot_name: 'chiwei',
        channel: 'lark',
        common_user_id: '0198f7c0-0000-7000-8000-000000000001',
        credentials: { app_id: 'cli_test' },
        init_type: 'websocket',
        is_active: true,
        is_dev: false,
        bot_role: 'persona',
        createdAt: new Date(0),
        updatedAt: new Date(0),
    } as BotConfig;
}

function roster(bots: BotConfig[]): BotRoster {
    return { getAllBotConfigs: () => bots };
}

const NO_WEBSOCKETS: IngressStatus = { expected: 0, connected: 0, bots: [] };

function appWith(
    bots: BotConfig[],
    webhookPaths: string[] = [],
    ingress: () => IngressStatus = () => NO_WEBSOCKETS,
) {
    return createLarkServiceApp({
        bots: roster(bots),
        inbound: inboundThatMounts(webhookPaths),
        ingress,
    });
}

describe('GET /api/health', () => {
    it('reports the service identity and the bots it is driving', async () => {
        const app = appWith([bot()]);

        const res = await app.request('/api/health');
        expect(res.status).toBe(200);

        const body = (await res.json()) as Record<string, unknown>;
        expect(body.status).toBe('ok');
        expect(body.service).toBe('lark-service');
        expect(body.bots).toEqual([
            {
                name: 'chiwei',
                channel: 'lark',
                app_id: 'cli_test',
                common_user_id: '0198f7c0-0000-7000-8000-000000000001',
                init_type: 'websocket',
                is_active: true,
            },
        ]);
    });

    it('answers before any bot is configured', async () => {
        const res = await appWith([]).request('/api/health');
        expect(res.status).toBe(200);
        expect((await res.json()).bots).toEqual([]);
    });
});

describe('GET /metrics', () => {
    it('serves the prometheus exposition format', async () => {
        const res = await appWith([]).request('/metrics');
        expect(res.status).toBe(200);
        expect(res.headers.get('content-type')).toContain('text/plain');
        const body = await res.text();
        expect(body).toContain('process_cpu_seconds_total');
        expect(body).toContain('http_requests_total');
    });

    it('counts requests that went through the app', async () => {
        const app = appWith([]);
        await app.request('/api/health');
        const body = await (await app.request('/metrics')).text();
        expect(body).toMatch(/http_requests_total\{[^}]*path="\/api\/health"[^}]*\} \d+/);
    });
});

// 骨架的价值在于「后面挂上来的东西自动拿到 trace / 错误处理 / metrics」。
// 这几条断言的就是这个装配点，而不是 health 端点本身。
describe('the app is a mount point for the lark ingress routes', () => {
    it('gives every response a trace id', async () => {
        const res = await appWith([]).request('/api/health');
        expect(res.headers.get('X-Trace-Id')).toBeTruthy();
    });

    it('echoes an inbound trace id instead of minting a new one', async () => {
        const res = await appWith([]).request('/api/health', {
            headers: { 'x-trace-id': 'trace-from-gateway' },
        });
        expect(res.headers.get('X-Trace-Id')).toBe('trace-from-gateway');
    });

    it('turns a throwing route into a 500 without leaking internals', async () => {
        const app = appWith([]);
        app.get('/boom', () => {
            throw new Error('lark sdk blew up with a token in the message');
        });

        const res = await app.request('/boom');
        expect(res.status).toBe(500);
        expect(await res.json()).toEqual({ error: 'Internal server error', code: 500 });
    });

    it('records metrics for routes mounted after creation', async () => {
        const app = appWith([]);
        app.post('/webhook/probe', (c) => c.json({ ok: true }));
        await app.request('/webhook/probe', { method: 'POST' });

        const body = await (await app.request('/metrics')).text();
        expect(body).toMatch(/http_requests_total\{[^}]*path="\/webhook\/probe"[^}]*\} 1/);
    });

    it('lets the Lark inbound mount its webhook routes', async () => {
        const app = appWith([], ['/webhook/chiwei/event', '/webhook/chiwei/card']);

        expect((await app.request('/webhook/chiwei/event', { method: 'POST' })).status).toBe(200);
        expect((await app.request('/webhook/chiwei/card', { method: 'POST' })).status).toBe(200);
    });

});

// 切流判据读的是这里。"进程起来了"和"真的在接飞书事件"必须分得开：SDK 的长连
// start() 不等首次连接成功，把两者混成一个绿灯，就可能在新服务根本没连上的时候
// 把旧入口停掉 —— 那段时间飞书消息无人接收，且没有任何告警。
describe('GET /api/ready', () => {
    it('is ready when this deployment is not supposed to hold a long connection', async () => {
        const res = await appWith([]).request('/api/ready');
        expect(res.status).toBe(200);
        expect((await res.json()).status).toBe('ready');
    });

    it('is not ready while a long connection it should hold is still connecting', async () => {
        const app = appWith([], [], () => ({
            expected: 1,
            connected: 0,
            bots: [{ botName: 'chiwei', state: 'connecting' }],
        }));

        const res = await app.request('/api/ready');
        expect(res.status).toBe(503);
        const body = await res.json();
        expect(body.status).toBe('not-ready');
        expect(body.websockets).toEqual({
            expected: 1,
            connected: 0,
            bots: [{ botName: 'chiwei', state: 'connecting' }],
        });
    });

    it('becomes ready once every expected connection is up', async () => {
        const app = appWith([], [], () => ({
            expected: 2,
            connected: 2,
            bots: [
                { botName: 'chiwei', state: 'connected' },
                { botName: 'utility', state: 'connected' },
            ],
        }));

        expect((await app.request('/api/ready')).status).toBe(200);
    });

    // 两条里连上一条不算就位：另一条 bot 的消息还是没人接。
    it('is not ready while only some of the connections are up', async () => {
        const app = appWith([], [], () => ({
            expected: 2,
            connected: 1,
            bots: [
                { botName: 'chiwei', state: 'connected' },
                { botName: 'utility', state: 'disconnected' },
            ],
        }));

        expect((await app.request('/api/ready')).status).toBe(503);
    });
});

// 存活与就绪分开：health 报"进程还活着"（一直 200，免得连接抖动把 Pod 重启掉），
// ready 报"能不能接飞书事件"。
describe('GET /api/health while the ingress is down', () => {
    it('stays 200 but tells the truth about the connections', async () => {
        const app = appWith([], [], () => ({
            expected: 1,
            connected: 0,
            bots: [{ botName: 'chiwei', state: 'disconnected' }],
        }));

        const res = await app.request('/api/health');
        expect(res.status).toBe(200);
        expect((await res.json()).websockets).toEqual({
            expected: 1,
            connected: 0,
            bots: [{ botName: 'chiwei', state: 'disconnected' }],
        });
    });
});
