import {
    asyncLocalStorage as baseAsyncLocalStorage,
    context as baseContext,
    BaseRequestContext,
} from '@inner/shared';
import { v4 as uuidv4 } from 'uuid';

/**
 * Extended request context with bot-specific fields
 */
export interface RequestContext extends BaseRequestContext {
    botName?: string;
}

// Re-export the base asyncLocalStorage for compatibility
export const asyncLocalStorage = baseAsyncLocalStorage;

/**
 * Extended context utilities with bot-specific methods
 */
export const context = {
    ...baseContext,
    getBotName: () => {
        const store = asyncLocalStorage.getStore() as RequestContext | undefined;
        return store?.botName || '';
    },
    getAll: () => {
        return (asyncLocalStorage.getStore() as RequestContext) || { traceId: '' };
    },
    set: (updates: Partial<RequestContext>) => {
        const current = (asyncLocalStorage.getStore() as RequestContext) || { traceId: '' };
        return { ...current, ...updates };
    },
    /**
     * 手动运行AsyncLocalStorage上下文，主要用于WebSocket模式
     */
    run: async <T>(contextData: RequestContext, callback: () => Promise<T>): Promise<T> => {
        return asyncLocalStorage.run(contextData, callback);
    },
    /**
     * 创建带有traceId和botName的上下文数据
     */
    createContext: (botName?: string, traceId?: string): RequestContext => {
        return {
            traceId: traceId || uuidv4(),
            botName,
        };
    },
};
