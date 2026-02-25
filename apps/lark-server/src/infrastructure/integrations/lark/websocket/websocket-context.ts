import { context } from '@middleware/context';
import { BotConfig } from '@entities/bot-config';

/**
 * WebSocket事件处理的上下文注入装饰器
 * 为WebSocket模式的事件处理函数注入traceId和botName
 */
export function withWebSocketContext<T extends any[]>(
    botConfig: BotConfig,
    handler: (...args: T) => Promise<void>,
): (...args: T) => Promise<void> {
    return async (...args: T): Promise<void> => {
        // 创建包含botName和traceId的上下文
        const contextData = context.createContext(botConfig.bot_name);

        // 在AsyncLocalStorage上下文中执行处理函数
        await context.run(contextData, async () => {
            try {
                await handler(...args);
            } catch (error) {
                console.error(`Error in WebSocket handler for bot ${botConfig.bot_name}:`, error);
                throw error;
            }
        });
    };
}

/**
 * 为WebSocket事件处理函数创建带上下文的void装饰器
 * 这个函数专门用于Lark SDK的事件处理，它们需要void返回类型
 */
export function createWebSocketVoidDecorator<T>(
    botConfig: BotConfig,
    asyncFn: (params: T) => Promise<void>,
): (params: T) => void {
    return function (params: T): void {
        console.info(
            `[${botConfig.bot_name}] receive event_type: ${(params as { event_type: string })['event_type']}`,
        );

        // 使用上下文装饰器包装异步函数
        const wrappedFn = withWebSocketContext(botConfig, asyncFn);

        wrappedFn(params).catch((err) => {
            console.error(`[${botConfig.bot_name}] Error in async operation:`, err);
        });
    };
}
