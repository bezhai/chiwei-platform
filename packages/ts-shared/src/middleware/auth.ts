import type { Context, Next } from 'koa';

/**
 * Options for bearer auth middleware
 */
export interface BearerAuthOptions {
    /**
     * Function to get the expected token
     * @default () => process.env.INNER_HTTP_SECRET
     */
    getExpectedToken?: () => string | undefined;
    /**
     * Custom error response
     */
    errorResponse?: {
        missingAuth?: { success: boolean; message: string };
        invalidToken?: { success: boolean; message: string };
    };
}

/**
 * Create a bearer authentication middleware for Koa
 * Validates Authorization: Bearer <token> header
 */
export function createBearerAuthMiddleware(options: BearerAuthOptions = {}) {
    const {
        getExpectedToken = () => process.env.INNER_HTTP_SECRET,
        errorResponse = {
            missingAuth: {
                success: false,
                message: 'Missing or invalid Authorization header',
            },
            invalidToken: {
                success: false,
                message: 'Invalid authentication token',
            },
        },
    } = options;

    return async (ctx: Context, next: Next) => {
        const authHeader = ctx.request.headers.authorization;

        if (!authHeader || !authHeader.startsWith('Bearer ')) {
            ctx.status = 401;
            ctx.body = errorResponse.missingAuth;
            return;
        }

        const token = authHeader.substring(7); // Remove 'Bearer ' prefix
        const expectedToken = getExpectedToken();

        if (token !== expectedToken) {
            ctx.status = 401;
            ctx.body = errorResponse.invalidToken;
            return;
        }

        await next();
    };
}

/**
 * Default bearer auth middleware using INNER_HTTP_SECRET env var
 */
export const bearerAuthMiddleware = createBearerAuthMiddleware();
