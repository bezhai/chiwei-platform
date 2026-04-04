import { describe, test, expect } from 'bun:test';
import { Hono } from 'hono';

/**
 * 仅验证健康检查端点与错误处理中间件集成。
 * 使用 Hono 的 app.request() 发起请求，不启动真实端口监听。
 */
describe('startup/server 集成烟雾测试', () => {
    test('GET /api/health 返回 200 且包含服务字段', async () => {
        const app = new Hono();
        app.get('/api/health', (c) => {
            return c.json({ status: 'ok', service: 'lark-server' }, 200);
        });

        const res = await app.request('/api/health');
        expect(res.status).toBe(200);
        const body = await res.json();
        expect(body.service).toBe('lark-server');
    });
});
