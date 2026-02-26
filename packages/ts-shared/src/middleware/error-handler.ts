import type { Context, Next } from 'koa';

/**
 * Options for error handler middleware
 */
export interface ErrorHandlerOptions {
    /**
     * Logger instance with warn and error methods
     * If not provided, errors will only be sent in response
     */
    logger?: {
        warn: (message: string, meta?: Record<string, unknown>) => void;
        error: (message: string, meta?: Record<string, unknown>) => void;
    };
}

/**
 * Application error class for expected/operational errors
 */
export class AppError extends Error {
    constructor(
        public statusCode: number,
        message: string,
        public isOperational = true,
    ) {
        super(message);
        this.name = 'AppError';
    }
}

/**
 * Create an error handler middleware for Koa
 * Catches downstream errors and returns unified JSON responses
 */
export function createErrorHandler(options: ErrorHandlerOptions = {}) {
    const { logger } = options;

    return async (ctx: Context, next: Next): Promise<void> => {
        try {
            await next();
        } catch (err: unknown) {
            const error = err as Error;

            // AppError: expected operational error
            if (error instanceof AppError) {
                ctx.status = error.statusCode;
                ctx.body = {
                    error: error.message,
                    code: error.statusCode,
                };
                logger?.warn('Operational error', { message: error.message });
                return;
            }

            // Unknown error: avoid leaking internal implementation details
            ctx.status = 500;
            ctx.body = {
                error: 'Internal server error',
                code: 500,
            };
            logger?.error('Unexpected error', {
                name: error?.name,
                message: error?.message,
                stack: error instanceof Error ? error.stack : undefined,
            });
        }
    };
}

/**
 * Default error handler (without logger)
 */
export const errorHandler = createErrorHandler();
