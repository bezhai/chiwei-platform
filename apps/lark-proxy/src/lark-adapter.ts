import type { Handler } from 'hono';
import type * as Lark from '@larksuiteoapi/node-sdk';
import { AESCipher } from '@larksuiteoapi/node-sdk';

export function adaptHono(
    dispatcher: Lark.EventDispatcher | Lark.CardActionHandler,
): Handler {
    return async (c) => {
        const data = await c.req.json();

        // 处理飞书 url_verification 挑战（配置 webhook URL 时触发）
        const encryptKey = (dispatcher as any).encryptKey as string;
        const plain = 'encrypt' in data && encryptKey
            ? JSON.parse(new AESCipher(encryptKey).decrypt(data.encrypt))
            : data;
        if (plain.type === 'url_verification') {
            return c.json({ challenge: plain.challenge });
        }

        const headers = Object.fromEntries(c.req.raw.headers);
        const result = await (dispatcher as any).invoke(
            Object.assign(Object.create({ headers }), data),
        );
        return c.json(result);
    };
}
