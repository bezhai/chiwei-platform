import type { Context, Next } from 'koa';
import logger from '@logger/index';
import { createErrorHandler } from '@inner/shared';

/**
 * 统一错误处理中间件
 * 使用 ts-common 的 createErrorHandler 并注入 logger
 */
export const errorHandler = createErrorHandler({
    logger: {
        warn: (message: string, meta?: Record<string, unknown>) => {
            logger.warn(message, meta);
        },
        error: (message: string, meta?: Record<string, unknown>) => {
            logger.error(message, meta);
        },
    },
});
