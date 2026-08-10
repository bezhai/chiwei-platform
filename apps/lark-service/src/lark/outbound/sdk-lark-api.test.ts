import { describe, expect, it } from 'bun:test';
import { LarkClient } from '@inner/lark-utils';
import { Readable } from 'node:stream';

import type { PostContent } from './post-content';
import {
    createSdkLarkApi,
    larkClientPool,
    type LarkApiClient,
    type LarkClientPool,
} from './sdk-lark-api';

const post: PostContent = { content: [[{ tag: 'md', text: '你好' }]] };

interface Recorded {
    sent: Array<{ receiveId: string; content: unknown; msgType: string }>;
    replied: Array<{ messageId: string; content: unknown; msgType: string; inThread?: boolean }>;
    deleted: string[];
    uploaded: unknown[];
}

function fakeClient(over: Partial<LarkApiClient> = {}): { client: LarkApiClient } & Recorded {
    const recorded: Recorded = { sent: [], replied: [], deleted: [], uploaded: [] };
    const client: LarkApiClient = {
        async send(receiveId, content, msgType) {
            recorded.sent.push({ receiveId, content, msgType });
            return { message_id: 'om_new' };
        },
        async reply(messageId, content, msgType, inThread) {
            recorded.replied.push({ messageId, content, msgType, inThread });
            return { message_id: 'om_reply' };
        },
        async deleteMessage(messageId) {
            recorded.deleted.push(messageId);
        },
        async uploadImage(stream) {
            recorded.uploaded.push(stream);
            return { image_key: 'img_v3_uploaded' };
        },
        ...over,
    };
    return { client, ...recorded };
}

function apiWith(client: LarkApiClient) {
    const pool: LarkClientPool = { current: () => client };
    return createSdkLarkApi(pool);
}

describe('createSdkLarkApi 发消息', () => {
    it('富文本按飞书的 post 形状发：zh_cn 包一层、msg_type=post', async () => {
        const fake = fakeClient();
        const sent = await apiWith(fake.client).sendPost('oc_chat', post);

        expect(fake.sent).toEqual([
            { receiveId: 'oc_chat', content: { zh_cn: post }, msgType: 'post' },
        ]);
        expect(sent.messageId).toBe('om_new');
    });

    it('平台没给 message_id 时如实留空，不兜成空串', async () => {
        const fake = fakeClient({ async send() {return {};} });
        expect(await apiWith(fake.client).sendPost('oc_chat', post)).toEqual({
            messageId: undefined,
        });
    });

    it('飞书那边抛出来的错误原样往上走，不吞', async () => {
        const fake = fakeClient({
            async send() {
                throw new Error('机器人不在群聊中');
            },
        });
        expect(apiWith(fake.client).sendPost('oc_chat', post)).rejects.toThrow('机器人不在群聊中');
    });
});

describe('createSdkLarkApi 回复消息', () => {
    it('回复挂在被回复那条消息上，inThread 透传', async () => {
        const fake = fakeClient();
        const sent = await apiWith(fake.client).replyPost('om_trigger', post, true);

        expect(fake.replied).toEqual([
            { messageId: 'om_trigger', content: { zh_cn: post }, msgType: 'post', inThread: true },
        ]);
        expect(sent.messageId).toBe('om_reply');
    });

    it('inThread=false 也照实传下去', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).replyPost('om_trigger', post, false);
        expect(fake.replied[0].inThread).toBe(false);
    });
});

describe('createSdkLarkApi 撤回', () => {
    it('把裸的 message id 交给飞书', async () => {
        const fake = fakeClient();
        await apiWith(fake.client).recall('om_to_delete');
        expect(fake.deleted).toEqual(['om_to_delete']);
    });

    it('撤不掉时抛（已撤过 / 超时限都走这条）', async () => {
        const fake = fakeClient({
            async deleteMessage() {
                throw new Error('消息已被撤回或删除');
            },
        });
        expect(apiWith(fake.client).recall('om_1')).rejects.toThrow('消息已被撤回或删除');
    });
});

describe('createSdkLarkApi 上传图片', () => {
    it('把字节流交给飞书，返回 image_key', async () => {
        const fake = fakeClient();
        const key = await apiWith(fake.client).uploadImage(Buffer.from('bytes'));

        expect(key).toBe('img_v3_uploaded');
        expect(fake.uploaded).toHaveLength(1);
        expect(fake.uploaded[0]).toBeInstanceOf(Readable);
    });

    it('平台没给 key 时返回 null，让上层降级', async () => {
        const fake = fakeClient({ async uploadImage() {return {};} });
        expect(await apiWith(fake.client).uploadImage(Buffer.from('x'))).toBeNull();
    });

    it('上传抛异常时照抛，由上层统一降级', async () => {
        const fake = fakeClient({
            async uploadImage() {
                throw new Error('upload exploded');
            },
        });
        expect(apiWith(fake.client).uploadImage(Buffer.from('x'))).rejects.toThrow(
            'upload exploded',
        );
    });
});

describe('接在真的 LarkClient 上', () => {
    // 上面那些用手写替身验的是本文件自己的口径。这一组换成真的 LarkClient（只把它
    // 私有持有的原生 SDK client 换掉），验的是**接起来之后**的行为 —— 飞书返回非 0
    // code 时错误确实会一路传到出站调用方，而不是被哪一层吃掉变成"发成功了"。
    function realClientWith(native: unknown): LarkClient {
        const client = new LarkClient({ appId: 'cli_test', appSecret: 'secret' });
        (client as unknown as { client: unknown }).client = native;
        return client;
    }

    it('飞书返回非 0 code 时，sendPost 抛', async () => {
        const api = apiWith(
            realClientWith({
                im: {
                    message: {
                        create: async () => ({ code: 230001, msg: 'bot is not in the chat' }),
                    },
                },
            }),
        );

        expect(api.sendPost('oc_1', post)).rejects.toThrow('bot is not in the chat');
    });

    it('飞书返回非 0 code 时，recall 抛', async () => {
        const api = apiWith(
            realClientWith({
                im: { message: { delete: async () => ({ code: 99991663, msg: 'already gone' }) } },
            }),
        );

        expect(api.recall('om_1')).rejects.toThrow('already gone');
    });

    it('上传走到真 LarkClient 也能取出 image_key', async () => {
        const api = apiWith(
            realClientWith({
                im: { v1: { image: { create: async () => ({ image_key: 'img_v3_real' }) } } },
            }),
        );

        expect(await api.uploadImage(Buffer.from('x'))).toBe('img_v3_real');
    });
});

const credentials = (appId: string) => ({
    app_id: appId,
    app_secret: 's',
    encrypt_key: 'e',
    verification_token: 'v',
    robot_union_id: `on_${appId}`,
});

describe('larkClientPool', () => {
    it('每个 bot 各一个客户端，认名字取', () => {
        const pool = larkClientPool(
            [
                { botName: 'chiwei', credentials: credentials('cli_a') },
                { botName: 'ayana', credentials: credentials('cli_b') },
            ],
            () => 'ayana',
        );

        const first = pool.current();
        expect(first).toBeInstanceOf(LarkClient);
        // 同一个 bot 每次拿到同一个客户端：SDK 客户端内部缓存 tenant token，每次新建
        // 等于每条消息都去换一次 token。
        expect(pool.current()).toBe(first);
    });

    it('上下文里没有 bot 身份时抛，绝不挑一个默认的发出去', () => {
        const pool = larkClientPool([{ botName: 'chiwei', credentials: credentials('cli_a') }], () => '');
        expect(() => pool.current()).toThrow(/no lark bot in context/);
    });

    it('上下文说的 bot 不在本进程里时抛', () => {
        const pool = larkClientPool(
            [{ botName: 'chiwei', credentials: credentials('cli_a') }],
            () => 'someone_else',
        );
        expect(() => pool.current()).toThrow(/someone_else/);
    });
});
