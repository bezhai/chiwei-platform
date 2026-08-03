// 入口一：HTTP webhook。
//
// 这里**不桩飞书 SDK**：报文经 SDK 的 EventDispatcher 真跑一遍再落到我们的 sink。
// 桩掉 SDK 就等于只测了自己写的那几行，而这条链路上最容易出错的恰恰是"SDK 认不认
// 这个报文"。

import { describe, expect, it } from 'bun:test';
import { Hono } from 'hono';

import { createLarkEventSink } from './event-sink';
import type { LarkEvent } from './lark-event';
import { registerLarkWebhook } from './webhook';

const credentials = {
    app_id: 'cli_1',
    app_secret: 'secret',
    // 空 encrypt_key = 飞书后台没开加密，报文是明文。开了加密的情况由 SDK 自己
    // 解密，不是本服务的逻辑。
    encrypt_key: '',
    verification_token: 'vtok',
    robot_union_id: 'on_bot',
};

function appWith() {
    const delivered: LarkEvent[] = [];
    const recorded: unknown[] = [];
    const app = new Hono();
    registerLarkWebhook(
        app,
        { botName: 'chiwei', credentials },
        createLarkEventSink({
            botName: 'chiwei',
            record: async (p) => void recorded.push(p),
            deliver: async (e) => void delivered.push(e),
        }),
    );
    return { app, delivered, recorded };
}

function post(app: Hono, path: string, body: unknown) {
    return app.request(path, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
    });
}

function messageEvent() {
    return {
        schema: '2.0',
        header: {
            event_id: 'e1',
            token: 'vtok',
            create_time: '1700000000000',
            event_type: 'im.message.receive_v1',
            app_id: 'cli_1',
        },
        event: {
            sender: { sender_type: 'user', sender_id: { open_id: 'ou_u', union_id: 'on_u' } },
            message: {
                message_id: 'om_1',
                chat_id: 'oc_1',
                chat_type: 'group',
                create_time: '1700000000000',
                message_type: 'text',
                content: '{"text":"hello"}',
            },
        },
    };
}

describe('registerLarkWebhook', () => {
    it('answers the URL verification challenge Lark sends when the webhook is configured', async () => {
        const { app } = appWith();
        const res = await post(app, '/webhook/chiwei/event', {
            type: 'url_verification',
            challenge: 'abc123',
            token: 'vtok',
        });

        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ challenge: 'abc123' });
    });

    it('carries a message event all the way to the sink', async () => {
        const { app, delivered } = appWith();
        const res = await post(app, '/webhook/chiwei/event', messageEvent());
        await Bun.sleep(1);

        expect(res.status).toBe(200);
        expect(delivered).toHaveLength(1);
        expect(delivered[0]!.type).toBe('im.message.receive_v1');
        expect(delivered[0]!.botName).toBe('chiwei');
        // SDK 把 header 和 event 拍平成一个对象，这正是解析层期待的形状。
        expect((delivered[0]!.payload as { message: { message_id: string } }).message.message_id)
            .toBe('om_1');
    });

    it('records the raw event for audit', async () => {
        const { app, recorded } = appWith();
        await post(app, '/webhook/chiwei/event', messageEvent());
        await Bun.sleep(1);
        expect(recorded).toHaveLength(1);
    });

    it('takes card actions on their own route', async () => {
        const { app, delivered } = appWith();
        const res = await post(app, '/webhook/chiwei/card', {
            schema: '2.0',
            header: {
                event_id: 'e2',
                token: 'vtok',
                create_time: '1700000000000',
                event_type: 'card.action.trigger',
                app_id: 'cli_1',
            },
            event: {
                operator: { open_id: 'ou_u', union_id: 'on_u' },
                action: { tag: 'button', value: { type: 'update-photo-card', tags: ['a'] } },
                context: { open_message_id: 'om_1', open_chat_id: 'oc_1' },
            },
        });
        await Bun.sleep(1);

        expect(res.status).toBe(200);
        expect(delivered).toHaveLength(1);
        expect(delivered[0]!.type).toBe('card.action.trigger');
        expect(
            (delivered[0]!.payload as { action: { value: { type: string } } }).action.value.type,
        ).toBe('update-photo-card');
    });

    // 路由按 bot 名分开：一个进程替多个飞书应用接消息，路径是它们唯一的区分。
    it('gives each bot its own pair of routes', async () => {
        const delivered: LarkEvent[] = [];
        const app = new Hono();
        for (const botName of ['chiwei', 'utility']) {
            registerLarkWebhook(
                app,
                { botName, credentials },
                createLarkEventSink({
                    botName,
                    record: async () => {},
                    deliver: async (e) => void delivered.push(e),
                }),
            );
        }

        await post(app, '/webhook/utility/event', messageEvent());
        await Bun.sleep(1);

        expect(delivered.map((e) => e.botName)).toEqual(['utility']);
        expect((await post(app, '/webhook/nobody/event', messageEvent())).status).toBe(404);
    });
});
