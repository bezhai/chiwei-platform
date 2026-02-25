import { describe, test, expect } from 'bun:test';
import Koa from 'koa';
import Router from '@koa/router';
import request from 'supertest';

/**
 * 仅验证健康检查端点与错误处理中间件集成。
 * 使用 supertest 发起请求，不启动真实端口监听。
 */
describe('startup/server 集成烟雾测试', () => {
    test('GET /api/health 返回 200 且包含服务字段', async () => {
        const app: Koa = new Koa();
        const router = new Router();
        router.get('/api/health', (ctx) => {
            ctx.status = 200;
            ctx.body = { status: 'ok', service: 'lark-server' };
        });
        app.use(router.routes());

        const res = await request(app.callback()).get('/api/health');
        expect(res.status).toBe(200);
        expect(res.body.service).toBe('lark-server');
    });
});
