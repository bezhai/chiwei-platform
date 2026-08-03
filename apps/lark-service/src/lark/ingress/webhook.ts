// 入口一：HTTP webhook。飞书把事件 POST 到 /webhook/{bot}/event，卡片回调到
// /webhook/{bot}/card。
//
// 验签和解密全部交给飞书 SDK —— 我们不自己算签名。SDK 的 dispatcher 收到报文后
// 会把 header 和 event 拍平成一个对象再交给回调，那正是解析层期待的形状。
//
// 路径按 bot 名分开：一个进程替多个飞书应用接消息时，路径是它们唯一的区分方式。
// 这个入口是**被动**的 —— 路由注册上了不代表有流量，实际有没有流量由 api-gateway
// 的规则决定。切换时"webhook 指向哪个服务"是网关那一侧的动作。

import { AESCipher, CardActionHandler, type EventDispatcher } from '@larksuiteoapi/node-sdk';
import type { Handler, Hono } from 'hono';

import type { LarkCredentials } from '../credentials';
import { createEventDispatcher } from './event-dispatcher';
import type { LarkEventSink } from './event-sink';

export interface LarkWebhookBot {
    botName: string;
    credentials: LarkCredentials;
}

/**
 * 把 SDK 的 dispatcher 接到 hono 上。
 *
 * 两件 SDK 没替我们做的事：
 *   1. 配置 webhook URL 时飞书会先发一个 url_verification 挑战，要原样回 challenge。
 *      开了加密的话挑战本身也是密文，所以要先解。
 *   2. dispatcher.invoke 从原型链上读 headers（SDK 的约定），故用 Object.create。
 */
function adaptToHono(dispatcher: EventDispatcher | CardActionHandler): Handler {
    return async (c) => {
        const body = await c.req.json();

        const encryptKey = (dispatcher as unknown as { encryptKey?: string }).encryptKey;
        const plain =
            'encrypt' in body && encryptKey
                ? JSON.parse(new AESCipher(encryptKey).decrypt(body.encrypt))
                : body;
        if (plain.type === 'url_verification') {
            return c.json({ challenge: plain.challenge });
        }

        const headers = Object.fromEntries(c.req.raw.headers);
        const invoke = (dispatcher as unknown as { invoke(data: unknown): Promise<unknown> }).invoke;
        const result = await invoke.call(dispatcher, Object.assign(Object.create({ headers }), body));
        return c.json(result as Record<string, unknown>);
    };
}

export function registerLarkWebhook(app: Hono, bot: LarkWebhookBot, sink: LarkEventSink): void {
    const eventDispatcher = createEventDispatcher(bot.credentials, sink);
    const cardHandler = new CardActionHandler(
        {
            verificationToken: bot.credentials.verification_token,
            encryptKey: bot.credentials.encrypt_key,
        },
        (payload: unknown) => sink.onCardAction(payload),
    );

    app.post(`/webhook/${bot.botName}/event`, adaptToHono(eventDispatcher));
    app.post(`/webhook/${bot.botName}/card`, adaptToHono(cardHandler));

    console.info(
        `[lark-ingress] ${bot.botName} (${bot.credentials.app_id}) ` +
            `→ /webhook/${bot.botName}/{event,card}`,
    );
}
