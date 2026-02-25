import { Context, Next } from 'koa';
import { asyncLocalStorage, context } from './context';

export const botContextMiddleware = async (ctx: Context, next: Next) => {
    // 从 X-App-Name header 获取 bot_name（由 lane-proxy 设置）
    const botName = (ctx.request.headers['x-app-name'] as string) || undefined;

    // 将 botName 注入到现有的 AsyncLocalStorage 上下文中
    const newStore = context.set({ botName });

    await asyncLocalStorage.run(newStore, next);
};
