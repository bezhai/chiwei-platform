import { describe, test, expect, beforeEach, mock } from 'bun:test';
import { Hono } from 'hono';
import { AppError } from '@inner/shared';

// Mock logger to assert calls
const mockLogger = {
    warn: mock(),
    error: mock(),
    info: mock(),
};
mock.module('@logger/index', () => ({
    default: mockLogger,
}));

const { errorHandler } = await import('@middleware/error-handler');

describe('middleware/error-handler', () => {
    beforeEach(() => {
        mockLogger.warn.mockReset();
        mockLogger.error.mockReset();
        mockLogger.info.mockReset();
    });

    test('捕获 AppError 并返回统一业务错误响应', async () => {
        const app = new Hono();
        app.onError(errorHandler);
        app.get('/test', () => {
            throw new AppError(400, '无效的参数');
        });

        const res = await app.request('/test');

        expect(res.status).toBe(400);
        expect(await res.json()).toEqual({ error: '无效的参数', code: 400 });
        expect(mockLogger.warn).toHaveBeenCalledWith('Operational error', {
            message: '无效的参数',
        });
        expect(mockLogger.error).not.toHaveBeenCalled();
    });

    test('捕获未知错误并返回 500 与通用消息', async () => {
        const app = new Hono();
        app.onError(errorHandler);
        app.get('/test', () => {
            throw new Error('boom');
        });

        const res = await app.request('/test');

        expect(res.status).toBe(500);
        expect(await res.json()).toEqual({ error: 'Internal server error', code: 500 });
        expect(mockLogger.error).toHaveBeenCalled();
    });
});
