import type { Context, Next } from 'hono';

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
 * Create a bearer authentication middleware for Hono
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

    return async (c: Context, next: Next) => {
        const authHeader = c.req.header('authorization');

        if (!authHeader || !authHeader.startsWith('Bearer ')) {
            return c.json(errorResponse.missingAuth, 401);
        }

        const token = authHeader.substring(7); // Remove 'Bearer ' prefix
        const expectedToken = getExpectedToken();

        if (token !== expectedToken) {
            return c.json(errorResponse.invalidToken, 401);
        }

        await next();
    };
}

/**
 * Default bearer auth middleware using INNER_HTTP_SECRET env var
 */
export const bearerAuthMiddleware = createBearerAuthMiddleware();
