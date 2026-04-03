import type { Context, Next } from 'hono';
import { asyncLocalStorage, context } from './context';

export const botContextMiddleware = async (c: Context, next: Next) => {
    // 从 X-App-Name header 获取 bot_name（由 lark-proxy 设置）
    const botName = c.req.header('x-app-name') || undefined;
    // 从 x-lane header 获取泳道标识（由 lark-proxy 设置）
    const lane = c.req.header('x-lane') || undefined;

    // 将 botName 和 lane 注入到现有的 AsyncLocalStorage 上下文中
    const newStore = context.set({ botName, lane });

    await asyncLocalStorage.run(newStore, next);
};
