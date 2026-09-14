import { afterAll, beforeAll, describe, expect, it } from 'bun:test';
import { Hono } from 'hono';
import { context } from '@inner/shared/middleware';
import { registerLarkDirectoryRoutes, DIRECTORY_SYNC_PATH } from './directory-routes';

const oldSecret = process.env.INNER_HTTP_SECRET;
beforeAll(() => {
    process.env.INNER_HTTP_SECRET = 'test-directory-secret';
});
afterAll(() => {
    if (oldSecret === undefined) delete process.env.INNER_HTTP_SECRET;
    else process.env.INNER_HTTP_SECRET = oldSecret;
});
function harness(fail = false) {
    const app = new Hono();
    const calls: unknown[] = [];
    registerLarkDirectoryRoutes(app, {
        lane: 'coe-members',
        hasBot: (name) => name === 'dev',
        sync: async (chatId, preview) => {
            calls.push({ chatId, preview, bot: context.getBotName(), lane: context.getLane() });
            if (fail) throw new Error('API denied');
            return { chatId, preview, joined: [{ unionId: 'on_new', name: '新人' }] };
        },
    });
    const request = (body: unknown, auth = true) =>
        app.request(DIRECTORY_SYNC_PATH, {
            method: 'POST',
            headers: {
                'content-type': 'application/json',
                ...(auth ? { authorization: 'Bearer test-directory-secret' } : {}),
            },
            body: JSON.stringify(body),
        });
    return { request, calls };
}
const valid = { bot_name: 'dev', chat_id: 'oc_1', preview: true };
describe('指定群成员同步入口', () => {
    it('未鉴权时不查询飞书也不写库', async () => {
        const h = harness();
        expect((await h.request(valid, false)).status).toBe(401);
        expect(h.calls).toEqual([]);
    });
    it('要求明确bot、chat以及预览或执行模式', async () => {
        const h = harness();
        for (const body of [
            {},
            { ...valid, preview: undefined },
            { ...valid, preview: 'false' },
            { ...valid, bot_name: 'missing' },
            { ...valid, chat_id: '' },
        ]) {
            expect((await h.request(body)).status).toBe(400);
        }
        expect(h.calls).toEqual([]);
    });
    it('使用指定bot和真实部署泳道，返回完整差异', async () => {
        const h = harness();
        const response = await h.request(valid);
        expect(response.status).toBe(200);
        expect(h.calls).toEqual([
            { chatId: 'oc_1', preview: true, bot: 'dev', lane: 'coe-members' },
        ]);
        expect(await response.json()).toEqual({
            lane: 'coe-members',
            result: {
                chatId: 'oc_1',
                preview: true,
                joined: [{ unionId: 'on_new', name: '新人' }],
            },
        });
    });
    it('明确preview false才执行写入', async () => {
        const h = harness();
        expect((await h.request({ ...valid, preview: false })).status).toBe(200);
        expect(h.calls).toEqual([expect.objectContaining({ preview: false })]);
    });
    it('查询失败返回失败', async () => {
        expect((await harness(true).request(valid)).status).toBe(500);
    });
});
