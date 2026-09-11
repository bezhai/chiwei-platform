// LarkClient 面向飞书 OpenAPI 的那一面：响应码怎么判、错误码怎么翻译、上传接口
// 的返回值怎么取。
//
// 测试把 LarkClient 私有持有的原生 SDK client 换成手写替身。构造 LarkClient 只读
// 配置、不建连接，所以这样做没有任何 I/O；而网络边界之外的一切仍然是真代码在跑 ——
// 要验的正是那部分。私有字段名 `client` 一旦改名，这里会立刻炸，是想要的。

import { describe, expect, it } from 'bun:test';
import { Readable } from 'node:stream';

import { LarkClient } from './client';
import { larkErrorCode } from './types';

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

// 飞书用数字码区分失败的种类，而业务层要按种类分开处置：撤回那一侧要把「消息已被撤回
// 或删除」跟「超出撤回时限」分开，前者意味着真人已经看不到这条消息、后者意味着它还在。
// 只给一句 msg 的话，业务层唯一能做的就是拿文案去匹配 —— 飞书改一个字就全错。
describe('抛出来的错误带着飞书的数字码', () => {
    /** 拿到抛出来的那个东西本身，而不是只看它的 message。 */
    async function thrownBy(run: () => Promise<unknown>): Promise<unknown> {
        try {
            await run();
        } catch (error) {
            return error;
        }
        throw new Error('expected the call to throw, but it returned');
    }

    it('HTTP 200 + 非 0 code：那个 code 挂在抛出的错误上', async () => {
        const client = clientWith({
            im: { message: { delete: async () => ({ code: 99991663, msg: 'raw msg' }) } },
        });

        expect(larkErrorCode(await thrownBy(() => client.deleteMessage('om_1')))).toBe(99991663);
    });

    it('SDK 抛 HTTP 错误那条路同样带码', async () => {
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

        expect(larkErrorCode(await thrownBy(() => client.deleteMessage('om_1')))).toBe(99991663);
    });

    it('不是飞书判的失败（网络断了）没有码 —— 不编一个出来', async () => {
        const client = clientWith({
            im: {
                message: {
                    delete: async () => {
                        throw new Error('socket hang up');
                    },
                },
            },
        });

        expect(larkErrorCode(await thrownBy(() => client.deleteMessage('om_1')))).toBeUndefined();
    });

    it('不是错误对象的东西问过来也答 undefined，不抛', () => {
        expect(larkErrorCode(undefined)).toBeUndefined();
        expect(larkErrorCode(null)).toBeUndefined();
        expect(larkErrorCode('99991663')).toBeUndefined();
        expect(larkErrorCode(new Error('plain'))).toBeUndefined();
    });

    // 带上这个码不许动现有调用方看得见的任何东西：它们只读 message、只 log 这个对象。
    it('类型、文案和可枚举字段都跟以前逐字一样', async () => {
        const client = clientWith({
            im: { message: { delete: async () => ({ code: 99991663, msg: 'raw msg' }) } },
        });

        const error = (await thrownBy(() => client.deleteMessage('om_1'))) as Error;

        expect(error).toBeInstanceOf(Error);
        expect(error.constructor).toBe(Error);
        expect(error.name).toBe('Error');
        expect(error.message).toBe('raw msg');
        // console.error(JSON.stringify(err)) 和 { ...err } 是现有调用方真的在做的事。
        expect(Object.keys(error)).toEqual([]);
        expect(JSON.stringify(error)).toBe('{}');
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
