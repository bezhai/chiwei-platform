import type { Context, Next } from 'hono';
import { asyncLocalStorage, type BaseRequestContext } from './context';

const CTX_PREFIX = 'x-ctx-';
const STORE_PREFIX = 'ctx:';

export function createContextPropagationMiddleware() {
    return async (c: Context, next: Next) => {
        const ctxFields: Record<string, unknown> = {};
        for (const [key, value] of Object.entries(c.req.header())) {
            if (key.startsWith(CTX_PREFIX)) {
                const fieldName = STORE_PREFIX + key.slice(CTX_PREFIX.length);
                ctxFields[fieldName] = value;
            }
        }

        const existing = asyncLocalStorage.getStore();
        if (existing) {
            Object.assign(existing, ctxFields);
            await next();
        } else {
            const store: BaseRequestContext = { traceId: '', ...ctxFields };
            await asyncLocalStorage.run(store, () => next());
        }
    };
}

export function getContextHeaders(): Record<string, string> {
    const store = asyncLocalStorage.getStore();
    if (!store) return {};

    const headers: Record<string, string> = {};
    for (const [key, value] of Object.entries(store)) {
        if (key.startsWith(STORE_PREFIX) && value != null) {
            const headerName = CTX_PREFIX + key.slice(STORE_PREFIX.length);
            headers[headerName] = String(value);
        }
    }
    return headers;
}
