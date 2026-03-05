import type { Handler } from 'hono';
import type * as Lark from '@larksuiteoapi/node-sdk';

export function adaptHono(
    dispatcher: Lark.EventDispatcher | Lark.CardActionHandler,
): Handler {
    return async (c) => {
        const data = await c.req.json();
        const headers = Object.fromEntries(c.req.raw.headers);
        const result = await (dispatcher as any).invoke(
            Object.assign(Object.create({ headers }), data),
        );
        return c.json(result);
    };
}
