import type { Context } from 'hono';
import type { ContentfulStatusCode } from 'hono/utils/http-status';

/**
 * Options for error handler
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
 * Create an error handler for Hono's app.onError()
 *
 * In Hono, errors thrown in route handlers are caught by the compose function
 * and forwarded to app.onError(), NOT to middleware try/catch. Therefore
 * error handling must use app.onError(handler) instead of middleware.
 */
export function createErrorHandler(options: ErrorHandlerOptions = {}) {
    const { logger } = options;

    return (err: Error, c: Context) => {
        // AppError: expected operational error
        if (err instanceof AppError) {
            logger?.warn('Operational error', { message: err.message });
            return c.json(
                {
                    error: err.message,
                    code: err.statusCode,
                },
                err.statusCode as ContentfulStatusCode,
            );
        }

        // Unknown error: avoid leaking internal implementation details
        logger?.error('Unexpected error', {
            name: err?.name,
            message: err?.message,
            stack: err instanceof Error ? err.stack : undefined,
        });
        return c.json(
            {
                error: 'Internal server error',
                code: 500,
            },
            500,
        );
    };
}

/**
 * Default error handler (without logger)
 */
export const errorHandler = createErrorHandler();
