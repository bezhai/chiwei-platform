import type { Handler } from 'hono';
import type * as Lark from '@larksuiteoapi/node-sdk';
import { AESCipher } from '@larksuiteoapi/node-sdk';

export function adaptHono(
    dispatcher: Lark.EventDispatcher | Lark.CardActionHandler,
): Handler {
    return async (c) => {
        try {
            const data = await c.req.json();
            console.info(`[adaptHono] received request, keys: ${Object.keys(data).join(',')}`);

            // 处理飞书 url_verification 挑战（配置 webhook URL 时触发）
            const encryptKey = (dispatcher as any).encryptKey as string;
            if ('encrypt' in data) {
                console.info(`[adaptHono] encrypted payload, encryptKey present: ${!!encryptKey}`);
            }
            const plain = 'encrypt' in data && encryptKey
                ? JSON.parse(new AESCipher(encryptKey).decrypt(data.encrypt))
                : data;
            if (plain.type === 'url_verification') {
                console.info(`[adaptHono] responding to url_verification challenge`);
                return c.json({ challenge: plain.challenge });
            }

            const headers = Object.fromEntries(c.req.raw.headers);
            const result = await (dispatcher as any).invoke(
                Object.assign(Object.create({ headers }), data),
            );
            console.info(`[adaptHono] invoke result: ${JSON.stringify(result)?.slice(0, 200)}`);
            return c.json(result);
        } catch (err) {
            console.error(`[adaptHono] error:`, err);
            return c.json({ error: 'internal' }, 500);
        }
    };
}
