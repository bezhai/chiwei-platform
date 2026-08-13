// LarkClient 面向飞书 OpenAPI 的那一面：响应码怎么判、错误码怎么翻译、上传接口
// 的返回值怎么取。
//
// 测试把 LarkClient 私有持有的原生 SDK client 换成手写替身。构造 LarkClient 只读
// 配置、不建连接，所以这样做没有任何 I/O；而网络边界之外的一切仍然是真代码在跑 ——
// 要验的正是那部分。私有字段名 `client` 一旦改名，这里会立刻炸，是想要的。

import { describe, expect, it } from 'bun:test';
import { Readable } from 'node:stream';

import { LarkClient } from './client';

function clientWith(native: unknown): LarkClient {
    const client = new LarkClient({ appId: 'cli_test', appSecret: 'secret' });
    (client as unknown as { client: unknown }).client = native;
    return client;
}

describe('LarkClient 的响应处理', () => {
    it('code=0 时返回信封里的 data', async () => {
        const client = clientWith({
            im: {
                message: {
                    create: async () => ({ code: 0, msg: 'ok', data: { message_id: 'om_1' } }),
                },
            },
        });

        expect(await client.send('oc_1', { text: 'hi' }, 'text')).toEqual({ message_id: 'om_1' });
    });

    it('code 非 0 时抛，不把失败当成功返回', async () => {
        const client = clientWith({
            im: {
                message: {
                    create: async () => ({ code: 230001, msg: 'bot is not in the chat' }),
                },
            },
        });

        expect(client.send('oc_1', { text: 'hi' }, 'text')).rejects.toThrow(
            'bot is not in the chat',
        );
    });

    it('撤回同样按 code 判定，非 0 抛', async () => {
        const client = clientWith({
            im: { message: { delete: async () => ({ code: 99991663, msg: 'raw msg' }) } },
        });

        expect(client.deleteMessage('om_1')).rejects.toThrow('raw msg');
    });

    it('SDK 抛出的 HTTP 错误里带已知飞书错误码时，翻译成人话', async () => {
        const client = clientWith({
            im: {
                message: {
                    delete: async () => {
                        throw Object.assign(new Error('Request failed'), {
                            response: { data: { code: 99991663, msg: 'message not found' } },
                        });
                    },
                },
            },
        });

        expect(client.deleteMessage('om_1')).rejects.toThrow('消息已被撤回或删除');
    });
});

describe('LarkClient.uploadImage', () => {
    // im.v1.image.create 是**上传接口**，SDK 直接返回解包后的 { image_key }，而不是
    // 普通 JSON 接口那个 { code, msg, data } 信封（见 @larksuiteoapi/node-sdk 的类型
    // 声明：`Promise<{ image_key?: string } | null>`）。当成信封去取 .data 会永远拿到
    // undefined —— 症状是每张图都"上传失败"，而且没有任何报错。
    it('返回 SDK 给的 image_key', async () => {
        const client = clientWith({
            im: { v1: { image: { create: async () => ({ image_key: 'img_v3_abc' }) } } },
        });

        expect(await client.uploadImage(Readable.from(Buffer.from('x')))).toEqual({
            image_key: 'img_v3_abc',
        });
    });

    it('SDK 返回 null 时给一个空对象，让调用方按"没拿到 key"处理', async () => {
        const client = clientWith({
            im: { v1: { image: { create: async () => null } } },
        });

        expect(await client.uploadImage(Readable.from(Buffer.from('x')))).toEqual({});
    });

    it('把流原样交给 SDK，并声明 image_type=message', async () => {
        const seen: Array<{ image: unknown; image_type: string }> = [];
        const client = clientWith({
            im: {
                v1: {
                    image: {
                        create: async (payload: { data: { image: unknown; image_type: string } }) => {
                            seen.push(payload.data);
                            return { image_key: 'img_v3_abc' };
                        },
                    },
                },
            },
        });

        const stream = Readable.from(Buffer.from('x'));
        await client.uploadImage(stream);

        expect(seen).toHaveLength(1);
        expect(seen[0].image).toBe(stream);
        expect(seen[0].image_type).toBe('message');
    });
});
