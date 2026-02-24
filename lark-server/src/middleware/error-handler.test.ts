import type { Context, Next } from 'koa';
import { errorHandler } from '@middleware/error-handler';
import { AppError } from '@inner/shared';
import logger from '@logger/index';

// Mock logger to assert calls
jest.mock('@logger/index', () => ({
    __esModule: true,
    default: {
        warn: jest.fn(),
        error: jest.fn(),
        info: jest.fn(),
    },
}));

describe('middleware/error-handler', () => {
    const getCtx = (): Context => ({
        // minimal ctx fields used by errorHandler
        status: 200,
        body: undefined,
    } as unknown as Context);

    const mockedLogger = logger as unknown as {
        warn: jest.Mock;
        error: jest.Mock;
        info: jest.Mock;
    };

    beforeEach(() => {
        jest.clearAllMocks();
    });

    test('捕获 AppError 并返回统一业务错误响应', async () => {
        const ctx = getCtx();
        const next: Next = (async () => {
            throw new AppError(400, '无效的参数');
        }) as Next;

        await errorHandler(ctx, next);

        expect(ctx.status).toBe(400);
        expect(ctx.body).toEqual({ error: '无效的参数', code: 400 });
        expect(mockedLogger.warn).toHaveBeenCalledWith('Operational error', { message: '无效的参数' });
        expect(mockedLogger.error).not.toHaveBeenCalled();
    });

    test('捕获未知错误并返回 500 与通用消息', async () => {
        const ctx = getCtx();
        const next: Next = (async () => {
            throw new Error('boom');
        }) as Next;

        await errorHandler(ctx, next);

        expect(ctx.status).toBe(500);
        expect(ctx.body).toEqual({ error: 'Internal server error', code: 500 });
        expect(mockedLogger.error).toHaveBeenCalled();
    });
});
