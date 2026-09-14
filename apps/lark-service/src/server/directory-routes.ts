import type { Hono } from 'hono';
import { bearerAuthMiddleware, context } from '@inner/shared/middleware';

export const DIRECTORY_SYNC_PATH = '/api/internal/lark/directory/sync';

export interface LarkDirectoryEndpoint {
    lane: string;
    hasBot(botName: string): boolean;
    sync(chatId: string, preview: boolean): Promise<unknown>;
}

/** 明确指定bot、群与预览模式。只操作请求到达的部署，不自动扩大同步范围。 */
export function registerLarkDirectoryRoutes(app: Hono, endpoint: LarkDirectoryEndpoint): void {
    app.post(DIRECTORY_SYNC_PATH, bearerAuthMiddleware, async (c) => {
        let body: Record<string, unknown>;
        try {
            body = await c.req.json();
        } catch {
            return c.json({ error: 'body must be JSON' }, 400);
        }
        if (
            !body ||
            typeof body !== 'object' ||
            Array.isArray(body) ||
            typeof body.bot_name !== 'string' ||
            !endpoint.hasBot(body.bot_name) ||
            typeof body.chat_id !== 'string' ||
            !body.chat_id ||
            typeof body.preview !== 'boolean'
        ) {
            return c.json(
                { error: 'registered bot_name, chat_id and boolean preview are required' },
                400,
            );
        }
        const { bot_name: botName, chat_id: chatId, preview } = body;
        const result = await context.run(
            context.createContext(context.getTraceId(), {
                botName,
                lane: endpoint.lane,
            }),
            () => endpoint.sync(chatId, preview),
        );
        console.info(
            `[lark-directory] manual sync bot=${botName} chat=${chatId} preview=${preview} lane=${endpoint.lane}`,
        );
        return c.json({ lane: endpoint.lane, result });
    });
}
