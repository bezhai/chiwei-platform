import type { Context, Next } from 'koa';
import { asyncLocalStorage, BaseRequestContext } from './context';
import { v4 as uuidv4 } from 'uuid';

/**
 * Options for trace middleware
 */
export interface TraceMiddlewareOptions {
    /**
     * Header name to read trace ID from
     * @default 'x-trace-id'
     */
    headerName?: string;
    /**
     * Response header name to set trace ID
     * @default 'X-Trace-Id'
     */
    responseHeaderName?: string;
    /**
     * Additional context fields to set
     */
    additionalContext?: (ctx: Context) => Partial<BaseRequestContext>;
}

/**
 * Create a trace middleware for Koa
 * Extracts or generates trace ID and runs the middleware chain within AsyncLocalStorage context
 */
export function createTraceMiddleware(options: TraceMiddlewareOptions = {}) {
    const {
        headerName = 'x-trace-id',
        responseHeaderName = 'X-Trace-Id',
        additionalContext,
    } = options;

    return async (ctx: Context, next: Next) => {
        const traceId = (ctx.request.headers[headerName] as string) || uuidv4();

        const contextData: BaseRequestContext = {
            traceId,
            ...(additionalContext ? additionalContext(ctx) : {}),
        };

        await asyncLocalStorage.run(contextData, async () => {
            ctx.set(responseHeaderName, traceId);
            await next();
        });
    };
}

/**
 * Default trace middleware with standard configuration
 */
export const traceMiddleware = createTraceMiddleware();
