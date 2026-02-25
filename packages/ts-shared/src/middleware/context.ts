import { AsyncLocalStorage } from 'async_hooks';
import { v4 as uuidv4 } from 'uuid';

/**
 * Base request context interface
 * Can be extended by applications for additional fields
 */
export interface BaseRequestContext {
    traceId: string;
    [key: string]: unknown;
}

/**
 * AsyncLocalStorage instance for request context
 */
export const asyncLocalStorage = new AsyncLocalStorage<BaseRequestContext>();

/**
 * Context utilities for accessing and managing request context
 */
export const context = {
    /**
     * Get the current trace ID from context
     */
    getTraceId: (): string => {
        const store = asyncLocalStorage.getStore();
        return store?.traceId || '';
    },

    /**
     * Get a specific field from context
     */
    get: <T = unknown>(key: string): T | undefined => {
        const store = asyncLocalStorage.getStore();
        return store?.[key] as T | undefined;
    },

    /**
     * Get all context data
     */
    getAll: (): BaseRequestContext => {
        return asyncLocalStorage.getStore() || { traceId: '' };
    },

    /**
     * Create updated context with new values (does not modify current context)
     */
    set: (updates: Partial<BaseRequestContext>): BaseRequestContext => {
        const current = asyncLocalStorage.getStore() || { traceId: '' };
        return { ...current, ...updates };
    },

    /**
     * Run a callback within a specific context
     * Primarily used for WebSocket mode or manual context management
     */
    run: async <T>(contextData: BaseRequestContext, callback: () => Promise<T>): Promise<T> => {
        return asyncLocalStorage.run(contextData, callback);
    },

    /**
     * Create a new context with traceId and optional additional fields
     */
    createContext: (traceId?: string, additionalFields?: Record<string, unknown>): BaseRequestContext => {
        return {
            traceId: traceId || uuidv4(),
            ...additionalFields,
        };
    },
};
