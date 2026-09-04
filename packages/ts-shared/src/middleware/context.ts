import { AsyncLocalStorage } from 'async_hooks';
import { v4 as uuidv4 } from 'uuid';

/**
 * Base request context interface
 * Can be extended by applications for additional fields
 */
export interface BaseRequestContext {
    traceId: string;
    // 处理这条请求/消息的 bot，以及它属于哪个泳道。两者都与渠道无关（任何
    // 渠道的入站都要回答"谁在处理"和"走哪条泳道"），且被共享的规则引擎直接
    // 消费，所以取值口径只在这里定义一次。
    botName?: string;
    lane?: string;
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
     * 当前处理这条请求/消息的 bot。无上下文时返回空串（调用方一律
     * `|| undefined` 转成"没有"），与 getTraceId 同口径。
     */
    getBotName: (): string => {
        const store = asyncLocalStorage.getStore();
        return store?.botName || '';
    },

    /**
     * 当前上下文所属泳道。prod 或无上下文时为空串。
     */
    getLane: (): string => {
        const store = asyncLocalStorage.getStore();
        return store?.lane || '';
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
    createContext: (
        traceId?: string,
        additionalFields?: Record<string, unknown>,
    ): BaseRequestContext => {
        return {
            traceId: traceId || uuidv4(),
            ...additionalFields,
        };
    },
};
