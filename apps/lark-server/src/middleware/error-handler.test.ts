import { describe, test, expect, beforeEach, mock } from 'bun:test';
import type { Context, Next } from 'koa';
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
    const getCtx = (): Context =>
        ({
            // minimal ctx fields used by errorHandler
            status: 200,
            body: undefined,
        }) as unknown as Context;

    beforeEach(() => {
        mockLogger.warn.mockReset();
        mockLogger.error.mockReset();
        mockLogger.info.mockReset();
    });

    test('捕获 AppError 并返回统一业务错误响应', async () => {
        const ctx = getCtx();
        const next: Next = (async () => {
            throw new AppError(400, '无效的参数');
        }) as Next;

        await errorHandler(ctx, next);

        expect(ctx.status).toBe(400);
        expect(ctx.body).toEqual({ error: '无效的参数', code: 400 });
        expect(mockLogger.warn).toHaveBeenCalledWith('Operational error', {
            message: '无效的参数',
        });
        expect(mockLogger.error).not.toHaveBeenCalled();
    });

    test('捕获未知错误并返回 500 与通用消息', async () => {
        const ctx = getCtx();
        const next: Next = (async () => {
            throw new Error('boom');
        }) as Next;

        await errorHandler(ctx, next);

        expect(ctx.status).toBe(500);
        expect(ctx.body).toEqual({ error: 'Internal server error', code: 500 });
        expect(mockLogger.error).toHaveBeenCalled();
    });
});
