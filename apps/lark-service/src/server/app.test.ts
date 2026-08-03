import { describe, expect, it } from 'bun:test';
import type { BotConfig } from '@inner/shared/entities';

import { createLarkServiceApp, type BotRoster } from './app';

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

describe('GET /api/health', () => {
    it('reports the service identity and the bots it is driving', async () => {
        const app = createLarkServiceApp({ bots: roster([bot()]) });

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
        const res = await createLarkServiceApp({ bots: roster([]) }).request('/api/health');
        expect(res.status).toBe(200);
        expect((await res.json()).bots).toEqual([]);
    });
});

describe('GET /metrics', () => {
    it('serves the prometheus exposition format', async () => {
        const res = await createLarkServiceApp({ bots: roster([]) }).request('/metrics');
        expect(res.status).toBe(200);
        expect(res.headers.get('content-type')).toContain('text/plain');
        const body = await res.text();
        expect(body).toContain('process_cpu_seconds_total');
        expect(body).toContain('http_requests_total');
    });

    it('counts requests that went through the app', async () => {
        const app = createLarkServiceApp({ bots: roster([]) });
        await app.request('/api/health');
        const body = await (await app.request('/metrics')).text();
        expect(body).toMatch(/http_requests_total\{[^}]*path="\/api\/health"[^}]*\} \d+/);
    });
});

// 骨架的价值在于「后面挂上来的东西自动拿到 trace / 错误处理 / metrics」。
// 这几条断言的就是这个装配点，而不是 health 端点本身。
describe('the app is a mount point for the lark ingress routes', () => {
    it('gives every response a trace id', async () => {
        const res = await createLarkServiceApp({ bots: roster([]) }).request('/api/health');
        expect(res.headers.get('X-Trace-Id')).toBeTruthy();
    });

    it('echoes an inbound trace id instead of minting a new one', async () => {
        const res = await createLarkServiceApp({ bots: roster([]) }).request('/api/health', {
            headers: { 'x-trace-id': 'trace-from-gateway' },
        });
        expect(res.headers.get('X-Trace-Id')).toBe('trace-from-gateway');
    });

    it('turns a throwing route into a 500 without leaking internals', async () => {
        const app = createLarkServiceApp({ bots: roster([]) });
        app.get('/boom', () => {
            throw new Error('lark sdk blew up with a token in the message');
        });

        const res = await app.request('/boom');
        expect(res.status).toBe(500);
        expect(await res.json()).toEqual({ error: 'Internal server error', code: 500 });
    });

    it('records metrics for routes mounted after creation', async () => {
        const app = createLarkServiceApp({ bots: roster([]) });
        app.post('/webhook/probe', (c) => c.json({ ok: true }));
        await app.request('/webhook/probe', { method: 'POST' });

        const body = await (await app.request('/metrics')).text();
        expect(body).toMatch(/http_requests_total\{[^}]*path="\/webhook\/probe"[^}]*\} 1/);
    });
});
